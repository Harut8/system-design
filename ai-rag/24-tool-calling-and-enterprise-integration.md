
# 24 — tool calling and enterprise integration

> **Prerequisites:** [`20-langchain-architecture-and-internals.md`](20-langchain-architecture-and-internals.md)
> (§8's claim that a tool's usefulness to a model is exactly as good as its type hints and
> docstring is this chapter's §3 taken to its production conclusion — schema quality is not a
> nicety here, it is the majority of your tool-selection-accuracy budget),
> [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md) (the `ToolNode`, checkpointing, and
> interrupt-for-approval machinery this chapter's §11.2 assumes as the durable substrate a
> confirmation gate suspends into), [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md)
> (§7's tool orchestration and §10's failure taxonomy are the agent-level view of exactly the
> mechanics this chapter works out at the single-tool-call level — read that chapter for "when does
> the loop call a tool," read this one for "what happens, in full, between the model deciding to
> call a tool and the model seeing the result"), and
> [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md)
> (bounded concurrency with cancellation and timeout propagation is not optional background here —
> §10's parallel tool dispatch and §9's timeout handling are that chapter's `asyncio.gather` and
> `asyncio.wait_for` patterns applied to code that calls real, money-moving, world-changing APIs).
> Useful but not required: [`../distributed-systems/README.md`](../distributed-systems/README.md)
> (idempotency, exactly-once-delivery myths, and circuit breakers are distributed-systems primitives
> first and agent-tooling primitives only by application — §8 and §9 of this chapter are that
> literature's vocabulary, not a new invention) and
> [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> (§9's tool-use-and-agent-traces span model is what §13 here wires a real tool call into).
>
> **Feeds into:** `14-agent-evaluation.md` (planned — tool-call correctness and argument accuracy,
> introduced in this chapter's §3.4 and §14, is one of the four trajectory-scoring axes that chapter
> formalizes), `16-multi-tenancy-and-isolation.md` (planned — §5.4's per-tenant tool scoping is that
> chapter's isolation requirement applied one layer up, at the tool registry instead of the index),
> `17-safety-guardrails-and-prompt-injection.md` (planned — §6's "the LLM's output is untrusted
> input" rule is that chapter's thesis, stated here first because you cannot reason about prompt
> injection until you have accepted that a tool call is a request, not a command),
> `19-build-vs-buy.md` (planned — §12's enterprise-integration-wrapper patterns are exactly the kind
> of plumbing that chapter will weigh against a vendor's pre-built connector catalog), and the P3/P4
> projects in this folder's [`README.md`](README.md), both of which are unbuildable without a real
> answer to every question in §6 and §8.
>
> **THESIS:** a tool call is the one place in an LLM application where a probabilistic process
> gets write access to the real world, and every design decision in this chapter follows from
> taking that sentence literally. The model's output — the tool name, the arguments, the timing,
> the decision to call at all — is not a command to execute. It is a proposal from an untrusted,
> occasionally-wrong, sometimes-adversarially-steered process, and it must clear exactly the same
> gauntlet you would put in front of a proposal from a stranger on the internet: schema validation,
> authorization scoped to a real principal, business-rule checks, idempotency, and an audit trail —
> all enforced by code the model does not control and cannot talk its way around. **The engineering
> job of tool calling is not "get the model to call the right function." It is "build the boundary
> that makes it safe to let a non-deterministic process request state changes in systems that don't
> forgive mistakes."** A platform engineer who ships one well-guarded transactional tool has done
> more for an organization's ability to deploy agents safely than one who has wired up fifty tools
> with no authorization layer between the model and the API key.

---

## Contents

1. [What tool calling is](#1-what-tool-calling-is)
2. [The tool calling protocol](#2-the-tool-calling-protocol)
3. [Tool definition and schemas](#3-tool-definition-and-schemas)
4. [The tool execution lifecycle](#4-the-tool-execution-lifecycle)
5. [Tool registries](#5-tool-registries)
6. [Authorization and security](#6-authorization-and-security)
7. [Validation](#7-validation)
8. [Idempotency](#8-idempotency)
9. [Error handling](#9-error-handling)
10. [Parallel tool calling](#10-parallel-tool-calling)
11. [Complex tool patterns](#11-complex-tool-patterns)
12. [Enterprise integration patterns](#12-enterprise-integration-patterns)
13. [Observability for tool calls](#13-observability-for-tool-calls)
14. [Testing tool-calling agents](#14-testing-tool-calling-agents)
15. [The cost of tool calling](#15-the-cost-of-tool-calling)
16. [Anti-patterns](#16-anti-patterns)
17. [Interview questions](#17-interview-questions)
18. [Lab exercises](#18-lab-exercises)

---

## 1. What tool calling is

Before function calling existed as an API primitive, getting an LLM to "do something" meant asking
it to produce text in a format your application parsed with regex or a hopeful `json.loads` call,
hoping the model didn't wrap the JSON in a markdown fence or add a chatty preamble. That approach
had a name — ReAct-style text parsing — and it worked, badly, at a cost that scaled with how
creative the model felt that day. Tool calling (OpenAI calls it function calling; Anthropic calls
it tool use; the underlying mechanism is the same) replaced "hope the text parses" with a real
protocol: the model is given a set of tool schemas alongside the prompt, and instead of free text
it can emit a structured object — a tool name and a set of arguments — that the *inference API
itself* guarantees is syntactically well-formed against the schema you supplied.

That guarantee is the entire value proposition, and it is worth being precise about what it does
and does not cover. The API guarantees the tool call is *syntactically* valid: the named tool
exists in the schema you passed, and the arguments are typed and shaped the way the schema says. It
guarantees nothing about *semantic* correctness — that the arguments are the ones you actually
wanted, that the tool is the right one for the user's intent, or that calling it is a good idea
right now. A model can emit a perfectly schema-valid call to `wire_transfer(amount=50000,
account="op-8842")` when the user asked to check their balance. Syntactic validity is necessary
and, on its own, worth almost nothing for safety. Keep that distinction in your head for the rest
of this chapter, because most of the machinery below — §6 through §9 in particular — exists purely
to cover the semantic gap the protocol does not.

### 1.1 The shift: from "generate text" to "decide and act"

Structurally, tool calling turns a single model call into a request for one of three outcomes:

- **A text response** — the model answers directly, no tool needed.
- **One or more tool calls** — the model has decided it needs external state or an external effect,
  and returns structured call(s) instead of, or alongside, text.
- **Both** — some providers allow a model to emit text ("Let me check that for you...") and a tool
  call in the same turn; others require you to loop back for the text after the tool result lands.

This is a genuine architectural shift, not a bigger prompt. A pure-generation LLM call is a pure
function: same input, (roughly) same output distribution, no side effects, trivially retriable. A
tool-calling turn converts the model from a text generator into a *dispatcher* — a component
deciding which of a fixed menu of effectful operations to invoke, with what arguments, and the
correctness of that dispatch decision is now part of your system's correctness surface in exactly
the way a hand-written `if/elif` router's correctness used to be, except the router is now a
probability distribution instead of code you wrote and can read.

### 1.2 The protocol-level mechanics

At the wire level, every tool-calling provider implements the same four-message dance:

```
1. Application → Model:   user message + list of available tool schemas
2. Model → Application:   assistant message containing tool_call(s) (name + arguments), no
                           final answer yet
3. Application → Tool:    application code executes the named function with the given arguments
4. Application → Model:   the tool's result, wrapped as a "tool result" message, appended to the
                           conversation and sent back
5. Model → Application:   a new assistant message — either the final answer, or another tool call
                           if the task needs more steps
```

The critical property to internalize: **the model never executes anything.** It only ever emits a
request to execute something. Every actual side effect — the HTTP call, the SQL write, the message
published to a queue — happens in your application code, in the step between messages 2 and 4
above. This is not an implementation detail; it is the single fact that makes tool calling safe to
reason about at all. If you remember nothing else from this chapter: **the boundary between "the
model decided" and "the system did" is a real process boundary, sitting entirely inside code you
own, and every section from §6 onward is about what has to happen inside that boundary before you
let step 3 run.**

```python
# The four-message dance, made concrete and minimal.
from openai import OpenAI

client = OpenAI()
messages = [{"role": "user", "content": "What's the weather in Boston?"}]

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a US city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name, e.g. 'Boston'"}},
            "required": ["city"],
        },
    },
}]

# 1. Application -> Model
response = client.chat.completions.create(model="gpt-4o", messages=messages, tools=tools)
msg = response.choices[0].message
messages.append(msg)

# 2. Model -> Application: msg.tool_calls is populated, msg.content is likely None
if msg.tool_calls:
    for call in msg.tool_calls:
        # 3. Application executes the tool — this is YOUR code, not the model's
        result = execute_tool(call.function.name, call.function.arguments)  # your dispatcher
        # 4. Application -> Model: the result, tagged with the tool_call_id it answers
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
    # 5. Model -> Application: send the augmented conversation back for the final answer
    final = client.chat.completions.create(model="gpt-4o", messages=messages, tools=tools)
```

Two things about this loop are easy to miss on a first read and expensive to miss in production.
First, `messages.append(msg)` — the assistant's tool-call message — must go back into the
conversation history verbatim, tool_call IDs and all, or the next turn's tool-result messages have
nothing to attach to and most providers will reject the request outright. Second, every tool result
message is tagged with a `tool_call_id`; when a model issues parallel calls (§10), you must return
one tool message per call, each tagged with its own ID, not one combined blob — providers validate
that the set of returned IDs exactly matches the set requested.

### 1.3 Why this is harder than it looks

The protocol is simple. The engineering problem is not the protocol. It's everything the protocol
is silent about: what happens when the model calls a tool it isn't authorized to call for this
user, what happens when the same "process this refund" call gets sent twice because a network
blip made the client retry, what happens when three parallel calls are issued and the second one
times out, what happens when the model calls `delete_customer_record` because a prompt-injected
document told it to. None of that is in the wire protocol. All of it is in this chapter.

---

## 2. The tool calling protocol

The two dominant providers — OpenAI and Anthropic — implement materially the same idea with
different wire formats, different defaults, and different rough edges. If you are building anything
provider-agnostic (and if you are on a platform team, you are), you need to know the differences
cold, because a normalization layer that gets one of these wrong produces a tool call that silently
never returns, not an error you can grep for.

### 2.1 OpenAI function calling format

Tools are declared as a list of `{"type": "function", "function": {...}}` objects, where the inner
object is a name, a description, and a JSON Schema for parameters:

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "create_purchase_order",
            "description": (
                "Create a new purchase order in the procurement system. Use this only after the "
                "user has confirmed the vendor, line items, and total amount explicitly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "string", "description": "Internal vendor ID, e.g. 'V-4471'."},
                    "line_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sku": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                                "unit_price_cents": {"type": "integer", "minimum": 0},
                            },
                            "required": ["sku", "quantity", "unit_price_cents"],
                        },
                    },
                    "currency": {"type": "string", "enum": ["USD", "EUR", "GBP"]},
                },
                "required": ["vendor_id", "line_items", "currency"],
                "additionalProperties": False,
            },
        },
    }
]
```

The model's response carries `message.tool_calls`, a list of objects each with an `id`, a
`function.name`, and `function.arguments` — **a JSON string, not a parsed object**. You must
`json.loads` it yourself, and you must handle the case where the model produced a string that
doesn't parse (rare with `strict: true` mode below, not rare without it).

OpenAI's **strict mode** (`"strict": true` inside the function object, requiring
`"additionalProperties": false` and every property listed in `required`) constrains decoding so the
output is guaranteed to match the schema exactly — this eliminates an entire class of "the model
put the amount in the wrong field" bugs at the cost of some flexibility (no true optional fields;
you model "optional" by making the type nullable and always emitting the key).

### 2.2 Anthropic tool use format

Anthropic's `tools` parameter uses `input_schema` instead of `parameters`, and the model's response
puts the tool call inside a **content block** of type `tool_use`, alongside a rendered `content`
list rather than a single `tool_calls` array:

```python
tools = [
    {
        "name": "create_purchase_order",
        "description": (
            "Create a new purchase order in the procurement system. Use this only after the "
            "user has confirmed the vendor, line items, and total amount explicitly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string", "description": "Internal vendor ID, e.g. 'V-4471'."},
                "line_items": {"type": "array", "items": {"type": "object", "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                    "unit_price_cents": {"type": "integer", "minimum": 0},
                }, "required": ["sku", "quantity", "unit_price_cents"]}},
                "currency": {"type": "string", "enum": ["USD", "EUR", "GBP"]},
            },
            "required": ["vendor_id", "line_items", "currency"],
        },
    }
]

response = anthropic_client.messages.create(model="claude-sonnet-4-5", max_tokens=1024,
                                             tools=tools, messages=messages)
for block in response.content:
    if block.type == "tool_use":
        name, args, call_id = block.name, block.input, block.id   # .input is already a dict

# The tool result goes back as a *user* message containing a tool_result content block —
# not a separate "tool" role, which Anthropic's API does not have.
messages.append({"role": "assistant", "content": response.content})
messages.append({"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": call_id, "content": result_str}
]})
```

Two format differences that matter operationally: Anthropic hands you `block.input` as an
**already-parsed dict**, so there's no `json.loads` step (and no "unparseable arguments" failure
mode to handle at that layer — though malformed *values inside* a valid dict are still entirely
possible and still your problem, see §7). And Anthropic has no separate `tool` role; the result
goes back as a `user`-role message with a `tool_result` block, which trips up anyone porting a
message-history abstraction built OpenAI-first.

### 2.3 tool_choice — auto, forced, and none

Both providers expose a `tool_choice` parameter controlling how strongly the model is steered
toward using a tool at all:

| Value | OpenAI | Anthropic | Effect |
|---|---|---|---|
| Auto | `"auto"` (default) | `{"type": "auto"}` | Model decides whether to call a tool or answer in text. |
| Forced, any tool | `"required"` | `{"type": "any"}` | Model must call *some* tool, but picks which. |
| Forced, specific tool | `{"type": "function", "function": {"name": "x"}}` | `{"type": "tool", "name": "x"}` | Model must call exactly this tool. |
| No tools | `"none"` | `{"type": "none"}` (or omit `tools`) | Model must answer in text, ignoring the tool list. |

Forced tool choice with a named tool is the workhorse for structured extraction dressed up as a
tool call — you don't actually want the model to *do* anything, you want it to fill out one schema
reliably, and forcing eliminates the failure mode where the model answers in prose instead of
calling the "tool." This is the standard trick for getting structured output before native
`response_format: json_schema` support existed, and it still shows up wherever the model needs to
call one of several possible "extraction shapes" depending on intent — force `tool_choice` to
`"required"`/`"any"` (pick among several) rather than to one name, and let the *choice* of which
schema fits still be the model's job.

### 2.4 Parallel tool calls

By default, current-generation models from both providers can emit *multiple* tool calls in a
single turn when the calls are independent — "get the weather in Boston and in Seattle" naturally
produces two `get_weather` calls in one assistant message rather than two separate round trips.
OpenAI exposes `parallel_tool_calls: false` to force strictly sequential, one-at-a-time calls (useful
when tool B's arguments causally depend on tool A's result and you'd rather the model literally
cannot try to guess both at once). Anthropic's models decide this natively based on task structure
and don't expose an equivalent kill switch as a top-level parameter as of this writing — you control
it by how you phrase tool descriptions and, if truly necessary, by disallowing tools that are safe
to combine. §10 covers the execution-side implications of this in full; the protocol-level point
here is just: **check `len(tool_calls) > 1` in your dispatcher unconditionally.** Code that
assumes exactly one tool call per turn works in every demo and breaks the first time a model
decides two independent lookups can be batched.

### 2.5 A provider-agnostic normalization layer

If you support both providers (or want to be able to switch), normalize immediately at the
boundary rather than threading provider-specific branches through your whole call stack:

```python
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

def normalize_openai_calls(message) -> list[NormalizedToolCall]:
    if not message.tool_calls:
        return []
    return [
        NormalizedToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
        for tc in message.tool_calls
    ]

def normalize_anthropic_calls(response) -> list[NormalizedToolCall]:
    return [
        NormalizedToolCall(id=b.id, name=b.name, arguments=b.input)
        for b in response.content if b.type == "tool_use"
    ]
```

Everything downstream of this point — the registry lookup, the authorization check, the execution
dispatcher — operates on `NormalizedToolCall` and never sees a provider-specific shape again. This
is the single highest-leverage refactor for any team maintaining tool-calling agents against more
than one model provider, and LangChain's `bind_tools` / `AIMessage.tool_calls` abstraction (covered
in `20` §8) is exactly this pattern, already built for you, if you're using the framework instead
of raw SDKs.

---

## 3. Tool definition and schemas

The schema is the entire interface between the model's reasoning and your code. It is not
documentation for humans that happens to also be machine-readable — for the model, it is the
*only* information it has about what the tool does, when to use it, and what arguments it expects.
Treat every field in a tool schema as a piece of prompt engineering, because that is exactly what
it is.

### 3.1 What makes a schema good

Four properties, in order of impact, from having watched tool-selection accuracy move on real
schema changes (this is `20` §8's claim, restated because this chapter is where it gets operationalized):

1. **The description answers "when do I use this," not "what does this do."** `"Get weather data"`
   tells the model nothing it couldn't infer from the name. `"Get the current weather conditions
   for a specific city. Use this when the user asks about current or recent weather — do NOT use
   this for weather forecasts more than 3 days out; use get_forecast for that."` disambiguates
   against the tool's most likely confusable neighbor, which is the actual job of the description.
2. **Every parameter has a description, and the description carries the constraint, not just the
   type.** `"amount": {"type": "number"}` is much weaker than `"amount": {"type": "number",
   "description": "Transaction amount in USD, must be positive, max 10000 without additional
   approval"}` — the model cannot see your business logic, but it can see, and act on, text in the
   schema.
3. **Enums over free strings wherever the value space is closed.** If `status` can only be
   `"pending"`, `"approved"`, or `"rejected"`, say so with `"enum": [...]`. This eliminates an
   entire failure mode (the model inventing `"in_review"` because it sounded plausible) for free,
   at zero runtime cost, purely by narrowing what the decoder is even allowed to sample.
4. **Required vs. optional is a real signal, not a formality.** Marking something required that is
   usually inferable makes the model hunt for or hallucinate a value it doesn't have; marking
   something optional that your business logic actually needs produces a tool call that passes
   schema validation and fails business validation three layers downstream, in a place with a much
   worse error message. Get this boundary right and half of §7's business-rule validation failures
   disappear before they happen.

### 3.2 LangChain's `@tool` decorator

For anyone building on LangChain (per `20`), the `@tool` decorator turns a type-hinted, docstringed
Python function directly into a schema — the docstring *is* the description, the type hints *are*
the JSON Schema types, derived via Pydantic under the hood:

```python
from langchain_core.tools import tool
from typing import Literal

@tool
def create_purchase_order(
    vendor_id: str,
    line_items: list[dict],
    currency: Literal["USD", "EUR", "GBP"],
) -> str:
    """Create a new purchase order in the procurement system.

    Use this only after the user has explicitly confirmed the vendor, every line item, and
    the total amount. Do not call this speculatively to "see what it would cost" — use
    price_purchase_order for that instead.

    Args:
        vendor_id: Internal vendor ID, formatted like 'V-4471'.
        line_items: List of {sku, quantity, unit_price_cents} dicts. Every field required.
        currency: ISO currency code for the order. Must match the vendor's contracted currency.
    """
    return _po_service.create(vendor_id, line_items, currency)
```

The `Args:` section of a Google-style docstring is parsed out and mapped, field by field, onto the
generated schema's per-property `description` — this is not cosmetic, it is the mechanism by which
your docstring becomes the model's schema documentation. Skipping the `Args:` section produces a
schema with types but no per-field descriptions, which is exactly the weakest of the three schema
qualities measured in this chapter's §18 Lab 1.

### 3.3 StructuredTool and tool-from-Pydantic-model

For tools whose input shape is complex enough to want independent testing and reuse, define the
schema as a standalone Pydantic model and wire it explicitly with `StructuredTool`, rather than
inferring it from a function signature:

```python
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

class CreatePurchaseOrderInput(BaseModel):
    vendor_id: str = Field(..., description="Internal vendor ID, formatted like 'V-4471'.")
    line_items: list[LineItem] = Field(..., description="Every line item on the order.", min_length=1)
    currency: Literal["USD", "EUR", "GBP"] = Field(..., description="Order currency.")
    notes: str | None = Field(None, description="Optional free-text note visible to the vendor.")

class LineItem(BaseModel):
    sku: str
    quantity: int = Field(..., gt=0)
    unit_price_cents: int = Field(..., ge=0)

def _create_po(vendor_id: str, line_items: list[LineItem], currency: str, notes: str | None = None) -> str:
    return _po_service.create(vendor_id, line_items, currency, notes)

create_po_tool = StructuredTool.from_function(
    func=_create_po,
    name="create_purchase_order",
    description="Create a new purchase order. Requires explicit user confirmation of every field.",
    args_schema=CreatePurchaseOrderInput,
)
```

This separation — schema as a named, independently importable Pydantic model, execution as a plain
function — is the pattern to standardize on for any tool that touches real state, for three
reasons that matter more as the tool count grows: the schema can be unit tested for validity on its
own (§14.1), it can be versioned independently of the function body (§5.2), and the same
`args_schema` can be reused to validate a tool call that arrives through a completely different
path — a REST endpoint, a queue message — with no duplication.

### 3.4 Complex nested schemas, and where they stop working

JSON Schema supports arbitrary nesting — objects containing arrays of objects containing enums —
and both providers will accept genuinely deep schemas. Model *accuracy* on deeply nested arguments
degrades well before the schema stops being syntactically valid, though, for a reason that has
nothing to do with schema validity: extracting five correctly-typed, correctly-nested fields from a
conversational description requires the model to hold five separate constraints in its generation
context simultaneously, and error compounds per field the way retrieval error compounds per hop
(`04` §13's point, restated one level up the stack). The practical ceiling, from measured behavior
across current frontier models, is roughly two levels of nesting with fewer than ten total leaf
fields before you should be flattening the schema, splitting the tool into two calls, or — the
option this chapter argues for in §11.1 — using a **preview-then-confirm** pattern that lets the
model build the structure incrementally across turns instead of in one shot.

A concrete smell: if you find yourself writing a `description` for a schema field that says
"required if X is set to Y" — conditional requiredness that JSON Schema's `required` array cannot
express directly (you'd need `if`/`then` composition, which most providers' schema validators
support only partially) — that is a signal the tool is doing two things and should be two tools.

---

## 4. The tool execution lifecycle

Spelling out every stage between "model decides" and "model sees a result" is the point of this
chapter, and every subsequent section is a deep dive into one stage of this pipeline:

```
User query
   │
   ▼
LLM inference call (with tool schemas attached)
   │
   ▼
tool_call decision            ← §2: model emits a NormalizedToolCall (name + raw arguments)
   │
   ▼
Schema validation             ← §7.1: do the arguments match the declared JSON Schema?
   │  fail → structured error back to model, no execution, no side effect
   ▼
Authorization check           ← §6: is THIS principal allowed to call THIS tool with THESE args?
   │  fail → UnauthorizedToolError, no execution, audit log written regardless
   ▼
Rate limit check              ← §12.2: has this principal/tool/tenant exceeded its budget?
   │  fail → RateLimitedError, model told to back off or ask the user to wait
   ▼
Business rule validation      ← §7.3: is this transaction within policy (amount, hours, scope)?
   │  fail → policy violation surfaced to model as a structured reason, not a generic failure
   ▼
Idempotency check             ← §8: has this exact operation already been executed?
   │  duplicate → return the ORIGINAL result, do not re-execute
   ▼
Tool execution                ← the actual HTTP call / SQL write / queue publish
   │  timeout / error → §9: structured error, retry policy, circuit breaker state update
   ▼
Result validation             ← §7.2: is the tool's response well-formed and safe to show the model?
   │
   ▼
ToolMessage constructed and appended to conversation
   │
   ▼
LLM inference call (with the tool result now in context)
   │
   ▼
Final response, or another tool_call if the task needs more steps
```

Two properties of this pipeline are worth stating explicitly because they are easy to erode as a
codebase grows. First, **every stage before "Tool execution" must be free of side effects.** A
system where authorization or validation code has any observable effect on real state means a
rejected call can still have changed something, which defeats the entire point of putting the
checks before execution. Second, **every stage produces a result that gets surfaced to the model in
some form**, not just the final one — a schema validation failure is not a `500` that ends the
turn, it is a structured message telling the model what was wrong with its arguments, fed back in
so the model can retry with corrected input. This is the mechanism (detailed in §7.4) by which a
well-built pipeline turns most of these failure stages into self-correcting loops instead of
task-ending crashes.

### 4.1 What can go wrong at each stage

| Stage | Failure mode | Typical cause | Right response |
|---|---|---|---|
| Tool call decision | Wrong tool selected | Ambiguous tool descriptions, too many similar tools (§16) | Improve schema, not a runtime fix |
| Tool call decision | Hallucinated tool name | Model trained/prompted with tools not in this session's list | Fuzzy-match, suggest real names, do not execute |
| Schema validation | Malformed arguments | Model error, or a schema too complex (§3.4) | Return validation error to model, allow retry |
| Authorization | Principal lacks scope | Correct behavior — the system working as designed | `UnauthorizedToolError`, audit log, no retry-with-different-args |
| Rate limit | Budget exceeded | Runaway loop, or legitimate high volume | Backoff signal to model or caller, not silent failure |
| Business rules | Amount over limit, out-of-policy | The tool call is syntactically fine but not allowed right now | Structured policy-violation reason, escalate to approval (§11.2) if applicable |
| Idempotency | Duplicate detected | Client retry, network blip, model re-issuing after a timeout | Return original result, log as dedup, do not re-execute |
| Execution | Timeout | Downstream API slow/down | §9's timeout + retry + circuit breaker, and critically — do NOT assume failure (§8.3) |
| Execution | Downstream 4xx/5xx | Bad request, downstream outage | Distinguish caller error (surface to model) from server error (retry/circuit-break) |
| Result validation | Malformed/oversized response | Downstream API contract drift | Reject, log, do not forward garbage into model context |
| Result validation | Sensitive data in response | PII/secrets in a field never meant for the model | Redact before constructing ToolMessage (§16) |

### 4.2 A reference implementation of the pipeline

This is the shape every stage below plugs into — a single `execute_tool_call` entrypoint that
every tool call in the system passes through, so that "add a policy check" or "add rate limiting"
is a one-file change, not a scattered one (this is `22` §13.3's platform-policy-engine argument,
applied concretely):

```python
from dataclasses import dataclass
from enum import Enum, auto

class ExecutionOutcome(Enum):
    SUCCESS = auto()
    SCHEMA_INVALID = auto()
    UNAUTHORIZED = auto()
    RATE_LIMITED = auto()
    POLICY_VIOLATION = auto()
    EXECUTION_ERROR = auto()
    DUPLICATE = auto()

@dataclass
class ToolExecutionResult:
    outcome: ExecutionOutcome
    payload: dict | None = None       # result data, on success or duplicate
    error_message: str | None = None  # structured, model-readable reason on failure
    retryable: bool = False

async def execute_tool_call(call: NormalizedToolCall, ctx: "ExecutionContext") -> ToolExecutionResult:
    tool = registry.get(call.name)
    if tool is None:
        return ToolExecutionResult(ExecutionOutcome.SCHEMA_INVALID, retryable=True,
                                    error_message=f"Unknown tool '{call.name}'. {registry.suggest(call.name)}")

    validated = tool.validate_input(call.arguments)          # §7.1
    if not validated.ok:
        return ToolExecutionResult(ExecutionOutcome.SCHEMA_INVALID, retryable=True,
                                    error_message=validated.error)

    if not await authorizer.check(ctx.principal, tool, validated.value):   # §6
        audit_log.record(ctx, tool, validated.value, outcome="unauthorized")
        return ToolExecutionResult(ExecutionOutcome.UNAUTHORIZED, retryable=False,
                                    error_message="Not authorized to perform this action.")

    if not await rate_limiter.allow(ctx.principal, tool):     # §12.2
        return ToolExecutionResult(ExecutionOutcome.RATE_LIMITED, retryable=True,
                                    error_message="Rate limit exceeded, retry after backoff.")

    policy_result = policy_engine.check(ctx, tool, validated.value)    # §7.3
    if not policy_result.ok:
        return ToolExecutionResult(ExecutionOutcome.POLICY_VIOLATION, retryable=False,
                                    error_message=policy_result.reason)

    idem_key = idempotency.key_for(ctx, tool, validated.value)   # §8
    cached = await idempotency.lookup(idem_key)
    if cached is not None:
        return ToolExecutionResult(ExecutionOutcome.DUPLICATE, payload=cached)

    try:
        raw_result = await circuit_breaker.call(tool, validated.value, ctx)   # §9
    except ToolExecutionError as e:
        return ToolExecutionResult(ExecutionOutcome.EXECUTION_ERROR, retryable=e.retryable,
                                    error_message=str(e))

    clean_result = tool.validate_output(raw_result)           # §7.2
    await idempotency.store(idem_key, clean_result)
    audit_log.record(ctx, tool, validated.value, outcome="success", result=clean_result)
    return ToolExecutionResult(ExecutionOutcome.SUCCESS, payload=clean_result)
```

Every subsequent section of this chapter is documentation for one line of this function.

---

## 5. Tool registries

Once a system has more than a handful of tools, "a list of Python functions imported into the
agent script" stops being an architecture. You need a **registry**: a single source of truth for
which tools exist, what they're scoped to, what version they're at, and who's allowed to call them
— because every one of §4's pipeline stages needs to ask the registry a question before it can act.

### 5.1 Why a central registry, concretely

Without one, four questions become unanswerable at scale: which agents currently have access to
`refund_payment`? Has this tool's schema changed since the eval suite was last run against it? What
does this tool cost, in dollars or rate-limit budget, per call? And — the one that actually causes
incidents — did someone add a new, unreviewed, high-privilege tool to an agent's tool list without
going through the same authorization review every other tool went through? A registry makes all
four a lookup instead of an archaeology exercise.

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class ToolMetadata:
    name: str
    version: str
    description: str
    args_schema: type[BaseModel]
    required_scopes: set[str]
    rate_limit_per_minute: int
    cost_estimate_usd: float          # per call, for budget attribution (see `11-token-accounting...`)
    is_destructive: bool              # drives the approval-gate decision in §11.2
    owner_team: str
    deprecated: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[ToolMetadata, callable]] = {}

    def register(self, metadata: ToolMetadata, fn: callable):
        if metadata.name in self._tools:
            existing, _ = self._tools[metadata.name]
            if existing.version == metadata.version:
                raise ValueError(f"Tool '{metadata.name}' v{metadata.version} already registered")
        self._tools[metadata.name] = (metadata, fn)

    def get(self, name: str) -> ToolMetadata | None:
        entry = self._tools.get(name)
        return entry[0] if entry else None

    def for_principal(self, principal: "Principal") -> list[ToolMetadata]:
        """Dynamic discovery — the model only ever sees the subset it's scoped to see."""
        return [
            meta for meta, _ in self._tools.values()
            if not meta.deprecated and meta.required_scopes.issubset(principal.granted_scopes)
        ]

    def suggest(self, unknown_name: str) -> str:
        """Fuzzy match for a hallucinated tool name — feeds `22` §10.4's recovery path."""
        import difflib
        matches = difflib.get_close_matches(unknown_name, self._tools.keys(), n=1)
        return f"Did you mean '{matches[0]}'?" if matches else "No similar tool exists."
```

### 5.2 Versioning

Tool schemas change — a required field gets added, an enum grows a new value, a parameter gets
renamed for clarity. Treat this exactly like an API contract, because to every deployed agent, it
is one:

- **Additive changes** (new optional field, new enum value) are backward compatible; bump a minor
  version, no migration needed.
- **Breaking changes** (renamed/removed field, a field made required) need a new tool *name* or a
  major version bump plus a deprecation window — an in-flight agent conversation that started with
  v1's schema in context should not suddenly be talking to v2's validator mid-task.
- Never mutate a tool's schema in place under load. An agent that had `create_order_v1`'s schema in
  its system prompt three tool calls ago and now gets validated against `create_order_v2`'s
  `required` list will fail in exactly the confusing way you'd expect — a call that looked valid
  when the model made the decision, rejected by validation that changed underneath it mid-task.

### 5.3 Dynamic discovery

The naive approach — hand every agent the entire tool catalog — actively hurts you (§16): the
model's tool-selection accuracy degrades as the candidate set grows, unrelated tools become
plausible-looking distractors, and every unauthorized tool sitting in the prompt is attack surface
even if the authorization layer would eventually reject a call to it. `for_principal` above is the
minimum bar: resolve the tool list *per request*, scoped to what this principal, in this tenant,
with this role, is actually allowed to touch — never a static list baked into the agent's system
prompt at build time.

### 5.4 Scoping per agent, user, and tenant

Three independent scoping dimensions, all enforced at the registry boundary, not downstream:

- **Per-agent** — a customer-support agent gets `lookup_order`, `issue_refund_under_50`; a
  billing-ops agent gets those plus `issue_refund_any_amount`. This is a deployment-time
  configuration, not a runtime decision.
- **Per-user** (the human on whose behalf the agent acts) — the agent's *own* service identity
  might technically be able to call `issue_refund_any_amount`, but the specific end user it's
  currently serving might only be entitled to refunds on their own orders. This is the OAuth
  token-forwarding case in §6.3 — the check has to be against the *human's* scope, not the agent
  service account's.
- **Per-tenant** — in a multi-tenant deployment, tool registration itself can differ: tenant A
  has a Salesforce integration enabled, tenant B has a HubSpot one, and the registry resolves
  `crm_lookup_contact` to a different implementation, with a different credential, per tenant,
  transparently to the agent's prompt (this is `16-multi-tenancy-and-isolation.md`'s subject
  applied one layer up from the vector index).

```python
def resolve_registry_for(principal: "Principal") -> ToolRegistry:
    base = global_registry.for_principal(principal)
    tenant_overrides = tenant_registry_overrides.get(principal.tenant_id, {})
    return apply_overrides(base, tenant_overrides)   # tenant-specific implementation swap
```

---

## 6. Authorization and security

This is the section that separates a demo from a system you can put in front of a government
transaction, a payment rail, or a customer's production database. Everything above this point has
been about correctness. This is about **trust boundaries**, and the single rule that generates
every sub-rule below is:

> **The LLM's output is untrusted input.** A tool_call is not different, from a security
> standpoint, from a form submission on a public web endpoint. It happens to be shaped like JSON
> and to have been produced by a very expensive process, but that process can be wrong, can be
> steered by content it read (prompt injection — see `22` §12.4), and has no concept of "am I
> authorized to do this" beyond text in its context that it has no mechanism to verify against
> reality. **Authorization is decided by code, against a real principal, every single time — never
> by the model, and never by anything the model said in its own output.**

### 6.1 Never let the LLM decide authorization

This sounds obvious stated baldly and is violated constantly in practice, usually implicitly:

```python
# WRONG — the "authorization check" is a string the model itself produced.
def refund_payment(order_id: str, amount: float, authorized_by: str) -> str:
    """authorized_by: the role of the person who approved this refund."""
    ...  # the model fills in authorized_by = "manager" because it decided that sounded right
```

Any tool parameter whose value is supposed to represent an authorization fact — who approved this,
what role the caller has, whether this was pre-approved — is a vulnerability if the model is the
one populating it. The model has no privileged channel to ground truth about who is actually
calling; it only has the conversation, which is exactly the surface a prompt injection or a
confused user can manipulate. **Authorization inputs come from the execution context your
application constructs — the authenticated session, the verified token — never from a tool
argument the model chose.**

```python
# RIGHT — the authorization fact comes from ctx, populated by your auth layer, not the model.
async def refund_payment(order_id: str, amount: float, *, ctx: ExecutionContext) -> str:
    if not authorizer.can(ctx.principal, "refund_payment", resource=order_id):
        raise UnauthorizedToolError(ctx.principal, "refund_payment", order_id)
    ...
```

Note `ctx` is not part of the tool's *model-visible* schema at all — it's injected by the execution
pipeline (§4.2) after the model's call is received, never a field the model can set.

### 6.2 Tool-level RBAC

Model authorization the same way you'd model authorization for any internal API — roles, scopes,
resource-level checks — and put the enforcement point at the registry/execution-pipeline boundary,
not scattered inside individual tool implementations where it's easy to forget on tool #47:

```python
@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    granted_scopes: frozenset[str]
    roles: frozenset[str]

class Authorizer:
    def __init__(self, policy_store: "PolicyStore"):
        self._policies = policy_store

    async def check(self, principal: Principal, tool: ToolMetadata, args: dict) -> bool:
        if not tool.required_scopes.issubset(principal.granted_scopes):
            return False
        # resource-level: does this principal own/have access to the specific resource in args?
        resource_rule = self._policies.resource_rule_for(tool.name)
        if resource_rule and not await resource_rule(principal, args):
            return False
        return True
```

A scope check answers "can this principal ever call `refund_payment`." A resource-level check
answers "can this principal call it *on this specific order*" — the difference between "billing
agents can issue refunds" and "this billing agent can issue a refund on an order it doesn't own,"
which is exactly the gap that turns a correctly-scoped tool into an IDOR vulnerability with extra
steps.

### 6.3 OAuth token forwarding — acting on behalf of a user

The pattern that separates "the agent's service account can do X" from "this specific human,
through the agent, can do X" is **token forwarding**: the agent never holds a standing credential
with the user's full privileges. Instead, the user's own OAuth token (or a narrowly-scoped
token exchanged for it, per RFC 8693 token exchange) is threaded through the execution context and
presented to the downstream API on every call, so the downstream system's own authorization applies
exactly as if the user had called it directly — the agent is a conduit, not a privilege escalation
path.

```python
async def crm_lookup_contact(contact_id: str, *, ctx: ExecutionContext) -> dict:
    # ctx.user_oauth_token was obtained during the user's own login flow, scoped to
    # crm:read for THIS user — never a shared service credential with crm:admin.
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://crm.internal/api/contacts/{contact_id}",
            headers={"Authorization": f"Bearer {ctx.user_oauth_token}"},
        )
        resp.raise_for_status()
        return resp.json()
```

This matters concretely: if the CRM's own access control says this user cannot see a given
contact, forwarding their token means the CRM enforces that — for free, correctly, without your
tool needing to reimplement CRM permission logic. Using a shared service-account token instead
means your tool has silently widened every user's effective access to whatever the service account
can do, a class of bug that is invisible in testing (the developer's own account usually has broad
access) and catastrophic in an audit.

### 6.4 Service-to-service auth

For tools calling internal services rather than acting on a specific user's behalf (a background
enrichment tool, an internal metrics lookup), use short-lived service credentials — mTLS, or
signed JWTs from a service identity provider (SPIFFE/SPIRE, or your cloud provider's workload
identity) — scoped as narrowly as the specific tool needs, never a long-lived static API key
checked into a secret store and shared across every tool in the registry. A single over-scoped
service credential is the reason a bug in tool #12 can exfiltrate data tool #12 was never supposed
to be able to reach.

### 6.5 Audit logging every tool call

Every stage of §4's pipeline — attempted, authorized, rejected, executed, failed — gets written to
an append-only audit log, independent of and in addition to any observability tracing (§13). The
distinction matters: observability is for engineers debugging behavior; an audit log is a
compliance and forensics artifact, and it needs to answer, months later, "who (which principal)
did what (which tool, which arguments), when, with what outcome, and under what authorization
decision" — including the rejected attempts, which are often the more important half of the log
during an incident review.

```python
@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    principal_id: str
    tenant_id: str
    tool_name: str
    tool_version: str
    arguments_redacted: dict          # PII/secret fields stripped per §16 before persisting
    outcome: str                      # "success" | "unauthorized" | "policy_violation" | "error"
    idempotency_key: str | None
    trace_id: str                     # links to the observability span, §13

class AuditLog:
    async def record(self, ctx: ExecutionContext, tool: ToolMetadata, args: dict,
                      outcome: str, result: dict | None = None) -> None:
        record = AuditRecord(
            timestamp=datetime.utcnow(), principal_id=ctx.principal.id, tenant_id=ctx.principal.tenant_id,
            tool_name=tool.name, tool_version=tool.version, arguments_redacted=redact(args, tool.pii_fields),
            outcome=outcome, idempotency_key=ctx.idempotency_key, trace_id=ctx.trace_id,
        )
        await self._append_only_store.write(record)   # never mutated, never deleted, per compliance retention
```

Write the audit record on the rejection path too, not only on success — "principal X attempted
`refund_payment` on an order they don't own and was denied" is exactly the record a security review
needs, and it does not exist if the audit hook only fires after successful execution.

---

## 7. Validation

Validation is not one check; it is three, at three different layers, catching three different
classes of error, and conflating them is how "the tool call passed validation" ends up meaning far
less than it sounds like.

### 7.1 Input validation — does the call match the schema?

This is Pydantic (or `jsonschema`) doing exactly what it's for: type coercion, required-field
checks, enum membership, numeric bounds declared in the schema. It catches "the model put a string
where a number goes" and "the model omitted a required field." It does **not** catch "the model
put a syntactically valid but business-nonsensical number in that field" — that's §7.3.

```python
from pydantic import ValidationError

def validate_input(schema: type[BaseModel], raw_args: dict) -> "ValidationResult":
    try:
        return ValidationResult(ok=True, value=schema.model_validate(raw_args))
    except ValidationError as e:
        # Render Pydantic's error into something a MODEL can act on, not a stack trace.
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        return ValidationResult(ok=False, error=f"Invalid arguments: {problems}")
```

The error message quality here directly determines whether the model self-corrects in one more
turn or spirals — "Invalid arguments: line_items.0.quantity: Input should be greater than 0" gives
the model exactly the edit to make; "ValidationError" does not.

### 7.2 Output validation — is the tool's response well-formed?

The tool executed successfully from the downstream system's point of view, but its response still
needs validation before it becomes something you hand back to the model: is the shape what your
tool's contract promises (a downstream API can silently change its response shape without you
noticing until a field access throws three calls later), is it a reasonable size (an unbounded
response can blow your context budget — see §15), and does it contain anything that needs
redaction before the model — and every downstream consumer of the model's next output, including
whatever renders that output to an end user — ever sees it (§16).

```python
def validate_output(schema: type[BaseModel], raw_result: dict) -> dict:
    validated = schema.model_validate(raw_result)          # shape contract
    payload = validated.model_dump()
    payload = truncate_if_oversized(payload, max_tokens=2000)   # §15's budget discipline
    payload = redact_sensitive_fields(payload)                  # §16
    return payload
```

### 7.3 Business rule validation — is this allowed, right now, for this transaction?

This is the layer that has nothing to do with types and everything to do with policy: is this
amount within this principal's approval limit, is this action allowed outside business hours, does
this transaction type require a second approver. Business rules live in a policy engine, not
scattered `if` statements inside tool bodies, for the same reason authorization does (§6.2) — a
policy that changes (the refund limit goes from $50 to $75) should be a data change in one place,
not a code change hunted down across every tool that happens to check an amount.

```python
class PolicyEngine:
    def __init__(self, rules: list["PolicyRule"]):
        self._rules = rules

    def check(self, ctx: ExecutionContext, tool: ToolMetadata, args: dict) -> "PolicyResult":
        for rule in self._rules:
            if rule.applies_to(tool.name):
                result = rule.evaluate(ctx, args)
                if not result.ok:
                    return result            # first violation wins; return its specific reason
        return PolicyResult(ok=True)

class AmountLimitRule:
    def applies_to(self, tool_name: str) -> bool:
        return tool_name in {"issue_refund", "create_purchase_order"}

    def evaluate(self, ctx: ExecutionContext, args: dict) -> "PolicyResult":
        limit = policy_store.limit_for(ctx.principal.roles)
        amount = args.get("amount") or args.get("total_cents", 0) / 100
        if amount > limit:
            return PolicyResult(ok=False, reason=f"Amount {amount} exceeds approval limit {limit} for role.")
        return PolicyResult(ok=True)
```

### 7.4 What to do when validation fails — retry-with-feedback vs. reject

Not every failure should go back to the model as "try again." The right response depends on which
layer failed:

- **Input validation failures are almost always retry-with-feedback.** The model made a mechanical
  mistake — a missing field, a type mismatch — and a clear error message plus another turn usually
  fixes it. Cap the retries (two, typically) so a persistently confused model doesn't burn an
  unbounded number of turns.
- **Authorization failures are never retry-with-feedback in the sense of "let the model try
  different arguments."** The model didn't make a mistake it can correct by adjusting its own
  output; the caller isn't allowed to do the thing. Surface this plainly ("You are not authorized
  to issue refunds over $50") so the model can *tell the user* rather than attempt a workaround —
  and specifically do not phrase the rejection in a way that hints at how to construct an
  authorized-looking call instead (see `22` §12.4's point about tool errors as an injection/probing
  surface).
- **Business rule failures are retry-with-feedback only if there's a valid alternative the model
  could reasonably construct** — "amount exceeds the $50 limit, ask the user to split this into two
  transactions or route to a manager" is actionable feedback; "policy violation" is not, and
  produces a model that either gives up unhelpfully or, worse, tries semantically identical
  arguments hoping the check was flaky.

```python
def build_tool_message(result: ToolExecutionResult, call: NormalizedToolCall) -> dict:
    if result.outcome == ExecutionOutcome.SUCCESS or result.outcome == ExecutionOutcome.DUPLICATE:
        content = json.dumps(result.payload)
    else:
        # Structured, actionable, and explicitly non-retryable failures say so —
        # this is the field the agent loop (`22` §4) reads to decide whether to try again.
        content = json.dumps({"error": result.error_message, "retryable": result.retryable})
    return {"role": "tool", "tool_call_id": call.id, "content": content}
```

---

## 8. Idempotency

This is the section that separates "we built an agent that calls APIs" from "we built an agent
safe to point at a payment rail or a government system." **Every tool that changes state needs an
idempotency story, and "we'll just be careful" is not one.**

### 8.1 The core problem

A tool call to `submit_tax_filing` goes out over the network. The request reaches the server, the
server processes it, submits the filing, and starts writing the response — and the connection
drops before the response arrives. Your code sees a timeout. **You now know one thing: you don't
know whether the filing was submitted.** Every naive response to this situation is wrong in a
different way: retrying blindly risks a duplicate filing; giving up and telling the user "it
failed" risks a filing that actually succeeded, followed by the user or the agent submitting it
again through some other path; and querying "did it work?" only helps if the downstream system
supports a status-check query in the first place (§8.4) — many legacy government and enterprise
systems don't.

### 8.2 At-most-once vs. at-least-once, and why neither is free

There is no execution semantics that gives you both "the effect definitely happened" and "the
effect definitely happened at most once" over an unreliable network without extra machinery — this
is not a tool-calling-specific limitation, it is the same exactly-once-delivery impossibility this
repo's `../distributed-systems/README.md` covers for message delivery generally, applied here to
"invoking a state-changing operation over HTTP." At-least-once (retry until success) risks
duplicates. At-most-once (never retry) risks silently dropping a legitimate request that actually
would have succeeded on retry. **The way out is not choosing one — it's making the operation
idempotent, so that at-least-once delivery composes safely with an exactly-once *effect*.**

### 8.3 Idempotency keys

The standard mechanism (Stripe's API popularized this pattern, and it's now table stakes for any
payment or transaction API): the caller generates a unique key *before* the first attempt, sends it
with every attempt of the *same logical operation*, and the server deduplicates on that key — a
retry with the same key returns the original result without re-executing the effect.

```python
import hashlib
import json

def generate_idempotency_key(ctx: ExecutionContext, tool_name: str, args: dict) -> str:
    """Deterministic within a single logical operation: same task, same tool, same args
    -> same key, so a retry of the SAME model-issued call collides with itself on purpose."""
    canonical = json.dumps({"task_id": ctx.task_id, "tool": tool_name, "args": args}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()

class IdempotencyStore:
    """Backed by a real datastore (Redis/Postgres) with a TTL matching the operation's
    realistic retry window — NOT an in-memory dict, which loses every in-flight key on
    the exact kind of crash this mechanism exists to survive."""

    async def lookup(self, key: str) -> dict | None:
        row = await self._db.fetch_one("SELECT status, result FROM idempotency_keys WHERE key = $1", key)
        if row is None:
            return None
        if row["status"] == "in_flight":
            # Another attempt is CURRENTLY executing this key — do not execute a second one.
            raise ConcurrentExecutionError(key)
        return row["result"]

    async def begin(self, key: str) -> None:
        await self._db.execute(
            "INSERT INTO idempotency_keys (key, status, created_at) VALUES ($1, 'in_flight', now()) "
            "ON CONFLICT (key) DO NOTHING", key)

    async def complete(self, key: str, result: dict) -> None:
        await self._db.execute(
            "UPDATE idempotency_keys SET status = 'complete', result = $2 WHERE key = $1",
            key, json.dumps(result))
```

Two details that separate a correct implementation from a plausible-looking broken one. First, the
key must be generated **before** the first network attempt and reused across every retry of that
same logical call — generating a fresh key per retry defeats the entire mechanism (this is the
single most common idempotency bug: retry logic that calls `generate_idempotency_key()` inside the
retry loop instead of once, outside it). Second, the `in_flight` state matters: without it, two
concurrent retries (one from a slow client timeout firing a retry while the original request is
still actually processing on the server) can both see "no result yet" and both execute — the
`in_flight` marker, written atomically before execution starts, is what makes concurrent duplicate
attempts serialize onto one execution instead of racing.

### 8.4 The status-check pattern, for systems that don't support idempotency keys

Plenty of enterprise and government integrations — legacy SOAP services in particular — don't
support client-supplied idempotency keys at all. For these, the pattern is: **before retrying,
check whether the operation already happened, using whatever query the system does support**, and
only submit if it demonstrably has not.

```python
async def submit_filing_with_status_check(filing: FilingRequest, ctx: ExecutionContext) -> dict:
    # The legacy system has no idempotency key support, but it does support a lookup by
    # (taxpayer_id, tax_year, filing_type), which is a valid natural key for "did this happen."
    existing = await legacy_client.query_filing_status(
        taxpayer_id=filing.taxpayer_id, tax_year=filing.tax_year, filing_type=filing.filing_type)
    if existing is not None and existing.status in ("submitted", "accepted"):
        return {"status": existing.status, "confirmation_number": existing.confirmation_number,
                "note": "Filing already existed; not resubmitted."}

    try:
        result = await legacy_client.submit_filing(filing)
        return {"status": "submitted", "confirmation_number": result.confirmation_number}
    except TimeoutError:
        # We STILL don't know if it went through. Poll the status endpoint with backoff
        # before concluding anything, rather than surfacing a false "it failed."
        for attempt in range(5):
            await asyncio.sleep(2 ** attempt)
            check = await legacy_client.query_filing_status(
                taxpayer_id=filing.taxpayer_id, tax_year=filing.tax_year, filing_type=filing.filing_type)
            if check is not None:
                return {"status": check.status, "confirmation_number": check.confirmation_number,
                        "note": "Original request timed out; confirmed via status check."}
        # After exhausting checks, this is a genuinely unresolved state — see §9.5's dead-letter
        # pattern. Do NOT tell the model or the user "it failed": say "unresolved, escalated."
        raise UnresolvedTransactionError(filing)
```

This is the answer to the classic interview question this chapter closes §17 with, worked out in
full: when a tool call times out and you cannot know whether it succeeded, the wrong moves are
"assume success" (you might duplicate a filing that never actually happened) and "assume failure
and retry blindly" (you might duplicate one that did). The right move is to check, if the system
supports checking; if it doesn't, escalate to a durable "unresolved, needs manual reconciliation"
state rather than guessing in either direction — an honest "I don't know" surfaced to a human is
categorically safer than a confident wrong answer in either direction, for a transaction a human
or a court will later hold someone accountable for.

### 8.5 Idempotency at the agent level, not just the tool level

One more layer up: if the *agent loop itself* retries a step after a crash and resume (`22` §8.3's
checkpoint-and-resume), the same idempotency key must survive the crash and be reused on resume —
generating a new key because the process restarted silently breaks the whole mechanism. Store the
idempotency key as part of the checkpointed task state (`21`'s LangGraph checkpointer is exactly
the place this belongs), keyed by a stable `task_id` plus step index, not by anything regenerated
at execution time.

---

## 9. Error handling

Tool execution fails in more distinct ways than a normal RPC call, because the caller is a model
that has to *understand* the failure well enough to decide what to do next, not just a process that
logs and moves on.

### 9.1 The taxonomy

- **Caller error** (4xx-equivalent) — the arguments were valid syntactically but wrong
  semantically (an order ID that doesn't exist). Surface directly to the model; often
  self-correctable in one more turn.
- **Timeout** — no error, no response, unknown state (§8.4's territory). The most dangerous
  category precisely because it looks the same whether the operation succeeded, is still running,
  or never started.
- **Downstream server error** (5xx-equivalent) — the API itself is failing. Not the model's
  problem to solve by changing arguments; this is a retry/circuit-breaker decision (§9.3), and
  telling the model "try different arguments" wastes a turn on an error no argument change fixes.
- **Partial failure in a parallel batch** — N tool calls issued together, M < N succeed. Needs
  explicit handling per call, not an all-or-nothing failure of the batch (§10.3).
- **Validation failure** — covered fully in §7; included here for completeness of the taxonomy.

### 9.2 Surfacing errors so the model can recover

The single highest-leverage change to error handling in a tool-calling system: **stop returning
exceptions or generic failure strings, and start returning structured, classified errors** that
tell the model what kind of failure this was and whether trying again (with the same or different
arguments) is a sane next move.

```python
@dataclass
class ToolError:
    kind: str            # "caller_error" | "timeout" | "server_error" | "unauthorized" | "policy"
    message: str          # human/model-readable explanation
    retryable: bool
    suggested_fix: str | None = None   # e.g. "check the order_id and retry"

def to_tool_message(call_id: str, error: ToolError) -> dict:
    return {
        "role": "tool", "tool_call_id": call_id,
        "content": json.dumps({
            "error": error.kind, "message": error.message,
            "retryable": error.retryable, "suggested_fix": error.suggested_fix,
        }),
    }
```

A model that receives `{"error": "timeout", "retryable": false, "message": "Downstream service
unavailable, do not retry automatically — a human has been notified."}` behaves completely
differently, and correctly, from one that receives a bare Python traceback string — which is what
"just `str(exception)` it into the tool message" produces in a codebase that never built this
layer.

### 9.3 Retry strategies

Retry the failure classes where retrying can plausibly help — timeouts and 5xx-equivalent errors —
with exponential backoff and a jitter, and **never** retry non-idempotent operations without the
§8 machinery in place first, because a naive retry of a state-changing call is exactly how a
duplicate charge happens.

```python
async def with_retry(fn, *, max_attempts=3, base_delay=0.5, retryable_exceptions=(TimeoutError, ServerError)):
    for attempt in range(max_attempts):
        try:
            return await fn()
        except retryable_exceptions as e:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            await asyncio.sleep(delay)
```

### 9.4 Circuit breakers for flaky tools

A single downstream dependency being down should degrade that one tool, not cascade into every
agent that touches it hammering a dead service with full-timeout retries. A circuit breaker per
tool (or per downstream host) trips after a failure threshold, short-circuits further calls
immediately (returning a fast, structured "temporarily unavailable" rather than waiting out a full
timeout each time), and periodically probes for recovery:

```python
import time
from enum import Enum, auto

class BreakerState(Enum):
    CLOSED = auto(); OPEN = auto(); HALF_OPEN = auto()

class ToolCircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout_s=30):
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._opened_at: float | None = None

    async def call(self, tool_fn, args, ctx):
        if self._state == BreakerState.OPEN:
            if time.monotonic() - self._opened_at < self._reset_timeout_s:
                raise ToolExecutionError("Tool circuit open — downstream unavailable.", retryable=True)
            self._state = BreakerState.HALF_OPEN     # allow one probe attempt

        try:
            result = await tool_fn(**args, ctx=ctx)
        except Exception:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
            raise
        else:
            self._failures = 0
            self._state = BreakerState.CLOSED
            return result
```

The failure this buys you back is exactly `22` §10.3's "the agent burns its entire iteration budget
retrying a dead dependency" pattern — with a breaker in place, the agent gets a fast, clearly-labeled
failure on the second or third call instead of a full-timeout stall on every one of ten calls,
which is the difference between a graceful degraded response and a hung task.

### 9.5 Dead-letter queues for failed tool calls

For state-changing tool calls that end in `UnresolvedTransactionError` territory (§8.4) or that
exhaust retries without a definitive outcome, write the full call context — arguments, idempotency
key, attempt history, last known state — to a dead-letter queue for manual reconciliation, rather
than letting the failure disappear into a log line no one will search for until a customer
complains.

```python
async def to_dead_letter(call: NormalizedToolCall, ctx: ExecutionContext, history: list[dict]) -> None:
    await dlq.publish({
        "tool": call.name, "arguments": call.arguments, "idempotency_key": ctx.idempotency_key,
        "principal_id": ctx.principal.id, "attempt_history": history,
        "requires_manual_reconciliation": True, "trace_id": ctx.trace_id,
    })
```

An on-call engineer (or a reconciliation job hitting the downstream system's own audit trail)
resolves these asynchronously. This is the operational safety net underneath every "we don't
actually know if it succeeded" case — it converts an invisible unknown into a queued, owned,
trackable item.

---

## 10. Parallel tool calling

Modern models frequently emit several tool calls in one turn when the calls are independent. How
you execute that batch is a real design decision, not a detail.

### 10.1 Detecting independence

Not every multi-call batch is safe to run concurrently. If call B's arguments look like they depend
on call A's *result* (rare — a well-formed batch from the model is independent by construction,
since the model can't see A's result before emitting both), the danger is actually the opposite
direction: two calls that are *independent in the model's intent* but **not independent in your
system's state** — two `update_inventory` calls touching the same SKU, issued together because the
user mentioned two changes in one message. Detecting this is a property of your own domain, not
something the protocol tells you: maintain a per-tool "conflicts with" declaration for any tool
that mutates shared state, and serialize (or reject one of) a batch containing a declared conflict.

```python
CONFLICTING_RESOURCE_EXTRACTORS = {
    "update_inventory": lambda args: ("inventory", args["sku"]),
    "issue_refund": lambda args: ("order", args["order_id"]),
}

def partition_batch(calls: list[NormalizedToolCall]) -> list[list[NormalizedToolCall]]:
    """Groups calls into batches that are safe to run in parallel; same-resource calls
    are pushed into sequential sub-batches to avoid a lost-update race."""
    seen_resources: dict[tuple, int] = {}
    batches: list[list[NormalizedToolCall]] = [[]]
    for call in calls:
        extractor = CONFLICTING_RESOURCE_EXTRACTORS.get(call.name)
        resource = extractor(call.arguments) if extractor else None
        if resource and resource in seen_resources:
            batches.append([call])           # forces this into its own, later, serial batch
            seen_resources = {resource: len(batches) - 1}
        else:
            batches[-1].append(call)
            if resource:
                seen_resources[resource] = len(batches) - 1
    return [b for b in batches if b]
```

### 10.2 Executing with bounded concurrency

Run each batch with `asyncio.gather`, but always through a semaphore-bounded dispatcher — per
`../python-mastery/29-async-patterns-and-pitfalls.md`'s core lesson, unbounded `gather` over
externally-triggered work (and a tool-call batch, coming from model output, counts as
externally-triggered) is a self-inflicted thundering-herd against whatever the tools call:

```python
async def execute_batch(calls: list[NormalizedToolCall], ctx: ExecutionContext,
                         max_concurrency: int = 5) -> list[ToolExecutionResult]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded(call: NormalizedToolCall) -> ToolExecutionResult:
        async with semaphore:
            return await execute_tool_call(call, ctx)

    return await asyncio.gather(*(bounded(c) for c in calls), return_exceptions=False)
```

`return_exceptions=False` is a deliberate choice here, not an oversight — `execute_tool_call`
already converts every failure mode into a `ToolExecutionResult`, never raising, so there should be
no bare exception reaching `gather` at all in the steady state. If one does, that's a bug in the
pipeline (an unhandled exception type), and it should fail loudly rather than being silently
absorbed as `return_exceptions=True` would do.

### 10.3 Handling partial failure

Do not let one failed call in a batch discard the successful results of the others. Build one tool
message per call, each carrying its own outcome, so the model sees exactly what succeeded and what
didn't and can reason about each independently:

```python
async def handle_parallel_calls(calls: list[NormalizedToolCall], ctx: ExecutionContext) -> list[dict]:
    for batch in partition_batch(calls):
        results = await execute_batch(batch, ctx)
        messages = []
        for call, result in zip(batch, results):
            messages.append(build_tool_message(result, call))   # §7.4 — one message per call, own outcome
        yield messages   # sequential batches; results within a batch return together
```

A model that gets back "get_weather(Boston) succeeded, get_weather(Seattle) timed out" can decide
to answer with what it has and note the gap, or retry just Seattle — a decision it cannot make if
your dispatcher collapsed the batch into one combined failure because one of two calls errored.

### 10.4 Timeout propagation across a batch

Give the whole batch a bounded wall-clock budget, not just each call individually, or one slow call
inside a fan-out can hold up the entire turn far longer than any single tool's own timeout would
suggest:

```python
async def execute_batch_with_deadline(calls, ctx, per_call_timeout=10.0, batch_deadline=15.0):
    async def with_timeout(call):
        try:
            return await asyncio.wait_for(execute_tool_call(call, ctx), timeout=per_call_timeout)
        except asyncio.TimeoutError:
            return ToolExecutionResult(ExecutionOutcome.EXECUTION_ERROR, retryable=True,
                                        error_message=f"{call.name} timed out after {per_call_timeout}s")

    try:
        return await asyncio.wait_for(
            asyncio.gather(*(with_timeout(c) for c in calls)), timeout=batch_deadline)
    except asyncio.TimeoutError:
        # The batch deadline fired even though individual calls had their own timeouts —
        # this is the concurrency-limit-too-low case; every in-flight task is cancelled here.
        raise BatchDeadlineExceeded(calls)
```

---

## 11. Complex tool patterns

Real tool-calling systems need more than "call a function, get a result" for the cases that matter
most operationally: multi-step workflows, human approval before an irreversible action, and
operations that don't complete within a single request/response cycle.

### 11.1 Multi-step tool workflows — search, select, execute, confirm

A common shape for anything transactional: don't give the model one giant tool that both finds and
acts on a target; give it a **search** tool that returns candidates, a natural language turn where
it (or the user) **selects** one, and a separate **execute** tool that acts on the selected,
now-unambiguous target. This decomposition does real work: it gives the user a checkpoint to
correct a wrong match before anything happens, and it means the "execute" tool's schema can take an
opaque, previously-validated ID instead of a fuzzy human description that has to be re-resolved
(and could resolve differently) at execution time.

```python
@tool
def search_customers(query: str) -> list[dict]:
    """Search for customers by name or email. Returns up to 5 candidates with IDs.
    Always show these to the user for confirmation before calling any action tool."""
    return customer_service.search(query, limit=5)

@tool
def issue_refund(customer_id: str, order_id: str, amount: float) -> dict:
    """Issue a refund. customer_id and order_id MUST come from a prior search_customers or
    lookup_order result — never invent these IDs."""
    ...
```

The docstring's explicit "never invent these IDs" instruction is doing real work here, but it is
not the actual safety mechanism — it's a nudge. The actual guarantee, if you need one, is that
`issue_refund`'s implementation independently verifies `order_id` exists and belongs to
`customer_id` before acting (§7.3's business-rule layer), so a hallucinated ID fails validation
regardless of what the prompt says.

### 11.2 Confirmation patterns — preview, approve, execute

For destructive or hard-to-reverse actions, split the tool into a **preview** step (compute what
would happen, return it, take no effect) and an **execute** step (actually do it), with a human
approval gate between them — the same suspend-and-resume mechanism as `22` §9.1's approval gate,
here scoped to a single tool rather than a whole task:

```python
@tool
def preview_purchase_order(vendor_id: str, line_items: list[dict], currency: str) -> dict:
    """Compute and return exactly what a purchase order WOULD look like — total cost, tax,
    estimated delivery — without creating anything. Always call this before create_purchase_order
    and show the result to the user."""
    return po_service.compute_preview(vendor_id, line_items, currency)

@tool
def create_purchase_order(preview_token: str) -> dict:
    """Actually create the purchase order previously computed by preview_purchase_order.
    Requires the exact preview_token from that call — this token expires in 10 minutes and
    is single-use, so a stale or reused token fails validation rather than silently
    re-executing a stale preview."""
    return po_service.commit_from_preview(preview_token)
```

`preview_token` is the load-bearing detail: it binds the eventual `create` call to the *exact*
previewed state (amounts, line items, computed tax) so that nothing can drift between what a human
approved and what actually executes, and its single-use, time-boxed nature gives you idempotency
(§8) as a side effect of the same mechanism — a replayed `create_purchase_order` call with an
already-consumed token is rejected, not re-executed.

### 11.3 Long-running tools — submit, poll, retrieve

Some operations — a batch data export, an ML training job, a large report generation — cannot
complete within a single tool-call round trip's latency budget. Model this explicitly as
submit/poll/retrieve rather than blocking the tool call itself for minutes:

```python
@tool
async def submit_report_job(report_type: str, date_range: dict) -> dict:
    """Submit a report generation job. Returns a job_id immediately; the report is NOT ready
    yet. Use check_job_status with the returned job_id to poll for completion."""
    job_id = await report_service.submit(report_type, date_range)
    return {"job_id": job_id, "status": "submitted", "estimated_seconds": 120}

@tool
async def check_job_status(job_id: str) -> dict:
    """Check the status of a previously submitted job. Poll this every 10-15 seconds until
    status is 'complete' or 'failed' — do not poll more frequently than that."""
    return await report_service.status(job_id)
```

Two things make this pattern work in an agent loop rather than devolving into a busy-poll spiral:
the tool's own docstring states the polling cadence explicitly (models will happily poll every
turn if not told otherwise, burning tokens on nothing), and the orchestration layer (LangGraph, per
`21`) should implement the actual wait as a durable, resumable suspend — not a `while True:
sleep(10)` inside the tool call itself, which blocks a worker for the job's entire runtime and
defeats checkpointing.

### 11.4 Streaming tool results

For a tool whose output is naturally incremental — a long file read, a live log tail — stream
partial results back rather than buffering the entire output before the model sees anything. Not
every provider's tool-calling protocol supports true intra-tool-call streaming to the model (the
model generally still consumes one complete tool-result message before continuing), so the common
production pattern is: **stream to the end user directly** (bypassing the model for the raw
stream) while the tool call itself returns a bounded summary or a reference the model can reason
about — "showed the user a live log tail; here are the last 20 lines and the error that appeared at
14:32" — rather than trying to force a token-by-token stream through the tool-result channel.

---

## 12. Enterprise integration patterns

This is where tool calling stops being an LLM-API topic and becomes systems integration — the same
discipline you'd apply building any service that calls REST APIs, GraphQL endpoints, databases,
queues, and legacy SOAP services, with the one addition that the caller triggering these
integrations is a model instead of a human clicking a button.

### 12.1 REST API wrappers

A tool wrapping a REST API should be a thin adapter around a properly-built API client — not
`httpx.get()` calls inline inside the `@tool`-decorated function — so the client's retry, auth, and
error-mapping logic is testable and reusable outside the tool-calling path entirely:

```python
class CRMClient:
    def __init__(self, base_url: str, token_provider: "TokenProvider", timeout: float = 10.0):
        self._base_url = base_url
        self._token_provider = token_provider
        self._client = httpx.AsyncClient(timeout=timeout)

    async def get_contact(self, contact_id: str) -> dict:
        token = await self._token_provider.get_token()      # handles refresh, per §12.4
        resp = await self._client.get(f"{self._base_url}/contacts/{contact_id}",
                                       headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 404:
            raise ContactNotFoundError(contact_id)
        resp.raise_for_status()
        return resp.json()

@tool
async def crm_lookup_contact(contact_id: str) -> dict:
    """Look up a CRM contact by ID."""
    try:
        return await crm_client.get_contact(contact_id)
    except ContactNotFoundError:
        return {"error": "not_found", "message": f"No contact with ID {contact_id}"}
```

The tool function is a thin translation layer: domain exceptions from the client become structured,
model-readable error payloads (§9.2), and nothing about HTTP, auth headers, or retries leaks into
the tool's own body.

### 12.2 Rate limiting external APIs

Two directions matter and are easy to conflate: **inbound** rate limiting (how often is this
principal/tenant allowed to invoke this tool — §4's pipeline stage, a policy decision) and
**outbound** rate limiting (respecting the *downstream API's own* rate limit, which is a property
of the integration, not the caller). Get outbound limiting wrong and one over-eager agent loop can
get your entire integration's shared API key throttled or banned, taking down the tool for every
other tenant and agent using it:

```python
class TokenBucketLimiter:
    def __init__(self, rate_per_second: float, burst: int):
        self._rate = rate_per_second
        self._tokens = burst
        self._max = burst
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._max, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._rate)
                self._tokens = 0
            else:
                self._tokens -= 1

# One shared limiter instance per downstream integration, not per tool call.
crm_api_limiter = TokenBucketLimiter(rate_per_second=10, burst=20)

async def get_contact_rate_limited(contact_id: str) -> dict:
    await crm_api_limiter.acquire()
    return await crm_client.get_contact(contact_id)
```

### 12.3 GraphQL, databases, message queues, and file systems as tool backends

The same wrapper discipline applies regardless of the backend shape, with the wrinkle each shape
brings:

- **GraphQL** — resist the temptation to expose the raw query as a model-constructed string
  parameter ("let the model write the GraphQL query"). This reopens the exact untrusted-input
  problem the whole schema-based tool-calling protocol exists to avoid — a model-authored query
  string is unstructured input again, with all of GraphQL's own injection and over-fetching
  surface. Expose specific, named operations (`get_order_with_line_items`) backed by pre-written,
  reviewed queries, with the tool's *arguments* being the only model-controlled part.
- **Databases** — never let a tool accept raw SQL from the model, for the same reason. Expose
  parameterized, named queries or an ORM-backed repository method per tool. If a use case
  genuinely needs open-ended querying (a "text-to-SQL" analytics agent), that's a distinct,
  much higher-risk pattern requiring a read-only replica, a query-cost limiter, and a result-row
  cap — treat it as a separate, more heavily gated tool class, not the default shape for a
  database-backed tool.
- **Message queues** — a tool that publishes to a queue is fire-and-forget from the tool's
  perspective, which makes idempotency (§8) and audit logging (§6.5) the *only* record that the
  action was requested at all; make sure the published message itself carries the idempotency key
  so a downstream consumer processing it twice (queues are typically at-least-once delivery) can
  also deduplicate.
- **File systems** — scope any file-system tool to a narrow, explicitly allow-listed directory
  root, validate every path argument against path traversal (`../../etc/passwd` is not a
  hypothetical when the path segment comes from model output shaped by document content it read),
  and treat file *content* returned by a read tool as untrusted input into the next model turn
  exactly like any other retrieved document (`22` §12.4).

### 12.4 Wrapping a legacy SOAP/REST API

Legacy enterprise systems — the kind actually running government transaction processing, core
banking, or ERP backends — are frequently SOAP, sometimes with cryptic fault codes, batch-oriented
semantics, and no idempotency-key support (§8.4's motivating case). The integration pattern is to
build an internal, modern adapter service in front of the legacy system once, and have your tool
call *that* adapter — not the legacy endpoint directly from tool code:

```python
class LegacyFilingAdapter:
    """Translates a clean, modern interface into the legacy SOAP calls, absorbing the
    legacy system's quirks (XML, fault codes, no idempotency support) in one place."""

    def __init__(self, soap_client: "ZeepClient", status_store: IdempotencyStore):
        self._soap = soap_client
        self._status_store = status_store

    async def submit_filing(self, filing: FilingRequest) -> "FilingResult":
        # Legacy SOAP faults are cryptic; translate them into the ToolError taxonomy (§9.1)
        # exactly once, here, rather than re-parsing fault codes in every tool that calls this.
        try:
            envelope = self._build_soap_envelope(filing)
            response = await self._soap.call("SubmitFiling", envelope)
        except SoapFault as fault:
            if fault.code in RETRYABLE_FAULT_CODES:
                raise ToolExecutionError("Legacy system transient fault", retryable=True) from fault
            raise ToolExecutionError(f"Filing rejected: {fault.readable_reason}", retryable=False) from fault
        return FilingResult.from_soap_response(response)
```

Building this adapter layer once, independent of the tool-calling code, means the legacy system's
XML/SOAP idiosyncrasies never leak into a `@tool`-decorated function's body, and it can be
unit-tested against recorded SOAP fixtures (§14.1) without any LLM involved at all.

### 12.5 Credential management

Never let a tool's own code hold or construct credentials inline. Every tool that calls an external
system resolves its credential through a dedicated secrets/token layer — scoped per-tenant where
applicable (§5.4), rotated on the provider's schedule rather than statically checked into
configuration, and never logged, including inside error messages or tracing spans (§13, §16). A
`TokenProvider` abstraction (as used in §12.1) that centralizes refresh logic means a credential
rotation or an OAuth token refresh is one implementation to get right, reused by every tool, instead
of copy-pasted token-refresh logic scattered across dozens of tool bodies with dozens of chances to
get the refresh race condition wrong.

---

## 13. Observability for tool calls

Every tool invocation should produce a trace span with a consistent schema, following
`../sre-observability/26-llm-and-ai-observability.md` §9's tool-use-and-agent-traces convention,
so tool call behavior is queryable across the whole fleet of agents rather than debuggable only by
reading application logs one incident at a time.

### 13.1 What to capture per call

```python
@dataclass
class ToolCallSpan:
    trace_id: str
    span_id: str
    tool_name: str
    tool_version: str
    principal_id: str
    tenant_id: str
    started_at: float
    ended_at: float | None
    outcome: str                    # matches ExecutionOutcome from §4.2
    input_tokens_equivalent: int    # size of arguments as sent, for cost attribution (§15)
    output_tokens_equivalent: int
    latency_ms: float
    retry_count: int
    idempotency_key: str | None
    error_kind: str | None
```

Wire this as an OpenTelemetry span (per the sre-observability GenAI semantic conventions), a child
of the parent LLM-call span, so a single trace shows the full turn: model call → tool span(s) →
model call, with per-tool latency and outcome visible in the same waterfall a request-latency trace
would show for any other service call.

### 13.2 The metrics that actually matter operationally

- **Latency per tool, p50/p95/p99** — a slow tool disproportionately drags overall task latency and
  is the first place to look when an agent's end-to-end time regresses.
- **Success/failure rate per tool, broken out by `ExecutionOutcome`** — a rising
  `POLICY_VIOLATION` rate for one tool is a signal the tool's schema/description doesn't match
  current policy, not necessarily a model regression; a rising `EXECUTION_ERROR` rate is a
  downstream-health signal, not an agent-quality one. Conflating these into one "tool call failure
  rate" number hides which system needs attention.
- **Tool calls per completed task** — a rising average, at constant task success rate, is
  frequently the earliest signal of a schema regression (a description got vaguer, an enum lost a
  value) making the model retry or re-plan more than it used to, well before it shows up as a
  success-rate drop.
- **Tool selection accuracy** — for a labeled eval set (per `22` §14.2), the fraction of tasks
  where the model called the *correct* tool for the step, independent of whether the overall task
  ultimately succeeded. This is the single most direct measurement of schema quality (§3.1) and
  should regress-gate any schema change in CI, the same way a retrieval eval regress-gates a
  chunking change (`08`'s discipline, applied here).
- **Token cost per tool-calling round** — covered in full in §15; the observability requirement is
  simply that this is captured per call, not estimated after the fact from aggregate billing.

### 13.3 Linking traces to the audit log

The `trace_id` on every `AuditRecord` (§6.5) and every `ToolCallSpan` is the same identifier,
deliberately — an incident investigation starts in the observability system to find *what*
happened and *when*, then pivots to the audit log using the shared trace ID to answer *who was
authorized to do it and under what policy decision*. Two systems, one join key, is the concrete
design point; a common mistake is building these as genuinely separate systems with no shared
identifier, which turns every serious incident review into manual timestamp correlation.

---

## 14. Testing tool-calling agents

Tool-calling systems have three genuinely different things that need testing, and conflating them
into one "test the agent" effort is how authorization bugs and schema regressions both ship
untested.

### 14.1 Unit testing tools — no LLM involved

Every tool's *implementation* is a plain function (or thin adapter, per §12) and should be unit
tested exactly like any other function: valid input produces correct output, invalid input is
rejected by the schema, business-rule edges (the exact policy limit, one cent over it) are covered,
and error paths (downstream timeout, downstream 4xx) are exercised against mocked clients — none of
this requires a model call, and none of it should wait on one.

```python
def test_issue_refund_rejects_over_policy_limit():
    ctx = make_test_context(principal_role="support_agent")   # limit = $50 for this role
    result = policy_engine.check(ctx, refund_tool_metadata, {"amount": 75.00})
    assert not result.ok
    assert "exceeds approval limit" in result.reason

def test_issue_refund_idempotent_on_duplicate_key():
    key = generate_idempotency_key(ctx, "issue_refund", {"order_id": "O-1", "amount": 20})
    first = await execute_tool_call(refund_call, ctx)
    second = await execute_tool_call(refund_call, ctx)   # same call, same key
    assert first.payload == second.payload
    assert refund_backend.call_count == 1     # NOT executed twice
```

### 14.2 Integration testing the tool-calling loop

One layer up: test the full pipeline from §4.2 against a real (or realistically faked) tool
backend, with the LLM call itself mocked out (§14.3) so the test is deterministic — this validates
that schema validation, authorization, idempotency, and error surfacing are correctly wired
*together*, independent of whether any particular model would have chosen to call the tool at all.

### 14.3 Mocking LLM responses for deterministic testing

Record real tool_call outputs from the model once, and replay them as fixtures — this is `22`
§14.5's replay-based regression pattern, applied at the single-turn level rather than the whole
trajectory:

```python
class MockLLMClient:
    def __init__(self, fixture_responses: list[NormalizedToolCall | str]):
        self._responses = iter(fixture_responses)

    async def next_turn(self, messages: list[dict]) -> NormalizedToolCall | str:
        return next(self._responses)

async def test_agent_recovers_from_schema_validation_error():
    mock_llm = MockLLMClient([
        NormalizedToolCall(id="1", name="create_purchase_order", arguments={"vendor_id": "V-1"}),  # missing required fields
        NormalizedToolCall(id="2", name="create_purchase_order", arguments=VALID_PO_ARGS),          # corrected
    ])
    result = await run_agent_turn(mock_llm, initial_message="Order 10 widgets from V-1")
    assert result.outcome == ExecutionOutcome.SUCCESS
    assert mock_llm.calls_made == 2   # confirms it took the retry-with-feedback path, not one-shot
```

This is what makes tool-calling behavior testable in CI without a live API key or non-deterministic
model sampling in the loop — the model's *decision* is fixed by the fixture; only the pipeline
around it (§4) is under test.

### 14.4 Golden-path and regression tests for tool schemas

Maintain a small, hand-labeled set of natural-language requests mapped to the expected tool name
and expected argument values (the same shape as `20` §18 Lab 8's measurement harness), and run it
against every schema change before merge — a schema edit that looks purely cosmetic (rewording a
description for clarity) can measurably shift tool-selection accuracy, and the only way to catch
that before production is to have the eval already built and gating CI.

### 14.5 Testing authorization boundaries explicitly

Write tests that assert the *negative* case as rigorously as the positive one: a principal lacking
a scope gets `UnauthorizedToolError`, not a degraded-but-still-executed call; a user-scoped OAuth
token that lacks access to a specific resource is rejected by the downstream system (or your own
resource-level check) even though the tool-level scope check passed; a tenant's tool registry
override never leaks a different tenant's credential. These tests are the ones that matter most in
an audit and are the ones most often missing, because a passing happy-path suite gives a false
sense of security about the boundary that actually protects the system.

---

## 15. The cost of tool calling

Every tool-calling round trip has a real, measurable token cost, and it compounds in a way that
plain-generation costs don't, because the entire conversation history — including every prior tool
call and every prior tool result — is resent as input on every subsequent turn.

### 15.1 The cost model

For a task requiring $n$ tool-calling rounds, the input token cost grows roughly with the *sum* of
all prior turns' content, not just the current turn's — turn $k$'s input includes the original
query, all $k-1$ prior tool calls, and all $k-1$ prior tool results, so total input tokens across
the task scale closer to $O(n^2)$ in the size of what's been accumulated than $O(n)$, even though
you only issued $n$ tool calls:

```python
def estimate_task_token_cost(num_rounds: int, avg_tool_result_tokens: int, base_prompt_tokens: int) -> int:
    total_input_tokens = 0
    accumulated = base_prompt_tokens
    for round_num in range(num_rounds):
        total_input_tokens += accumulated       # this round resends everything accumulated so far
        accumulated += avg_tool_result_tokens + 50   # + the tool call itself, roughly
    return total_input_tokens
```

This is why an agent that needed 3 tool calls in the first architecture iteration and needs 8 after
a schema regression (§13.2's leading indicator) doesn't cost "roughly 2.7x more" — it can easily
cost 5-8x more, because every one of those extra rounds resends everything before it.

### 15.2 Minimizing rounds

The concrete levers, in the order they're usually worth pulling:

- **Batch information into fewer, richer tool calls** rather than several narrow ones — a
  `get_customer_full_profile` call returning contact info, order history, and support tickets in
  one response beats three separate lookups if the task usually needs all three, even though it
  costs more on tasks that only needed one (measure the actual usage distribution before deciding;
  see the `08`/`22`-style discipline of not guessing).
- **Use parallel calls (§10) instead of sequential ones** wherever independence allows — parallel
  calls in the same turn don't compound the resend cost the way sequential turns do, since they
  share one round trip's worth of prior-context resend.
- **Truncate and summarize large tool results before they enter context** (§7.2's output validation
  layer is exactly where this belongs) — a tool that returns a 50KB JSON blob when the model needed
  three fields from it pays the resend cost of the other 49.9KB on every subsequent turn for the
  rest of the task.
- **Prompt caching** (covered in the `claude-api` reference material and `12-serving-latency-and-
  caching.md`, planned) — providers that support caching the stable prefix of a growing tool-call
  conversation can eliminate most of the re-send cost for the *unchanged* portion of history, which
  matters precisely because of the $O(n^2)$ accumulation pattern above.

### 15.3 When fewer, bigger calls stop being a win

The opposite failure mode exists too: cramming unrelated operations into one mega-tool to save
round trips produces exactly the schema-complexity problem in §3.4 — a tool with fifteen optional
parameters covering five different use cases is harder for the model to use correctly than five
focused tools, and the accuracy loss from a confused tool call plus a retry-with-feedback round
(§7.4) frequently costs more, in both tokens and wall-clock time, than the extra round trip a
second focused tool would have cost. Measure tool-selection accuracy (§13.2) before and after any
consolidation — this is an empirical tradeoff, not a rule of thumb to apply blindly in either
direction.

---

## 16. Anti-patterns

**Too many tools.** Beyond roughly 15-20 tools in a single model's active tool list, selection
accuracy degrades measurably regardless of provider, because the model has to discriminate against
an increasingly crowded, increasingly similar-looking candidate set on every decision. Fix with
dynamic, per-request scoping (§5.3) — resolve the tool list to what this task plausibly needs, not
the entire catalog every agent could theoretically touch.

**Vague tool descriptions.** `"Manage customer data"` is not a description, it's a category. Every
description should answer "when do I call this instead of its nearest confusable neighbor" (§3.1)
— if you can't name the neighbor, the tool probably doesn't need to exist as currently scoped.

**No input validation.** Trusting that schema-valid arguments are safe arguments (§7.1 vs §7.3
conflated) is how a syntactically perfect but business-nonsensical tool call — a refund for
$50,000, a filing for a taxpayer ID that doesn't match the authenticated session — reaches
execution.

**Trusting LLM output as authorized.** Covered at length in §6.1; worth restating as the anti-pattern
because it is the single most consequential mistake on this list. Any tool that reads an
authorization-relevant fact (who approved this, what role the caller has) from a model-populated
argument instead of the execution context is one prompt injection away from a privilege escalation.

**No idempotency on state-changing tools.** "We'll just tell people not to double-click" is not a
mitigation for a distributed system where the client and server can disagree about whether a
request landed (§8.1). Every tool that writes state needs an idempotency story before it needs
anything else in this chapter.

**No timeout on tool execution.** A tool call with no timeout means one hung downstream dependency
can hold an entire agent turn (and, in a synchronous serving architecture, the worker handling it)
indefinitely. Every tool call gets an explicit timeout, tuned to the operation's realistic latency,
never left at a language or library default that was chosen for an unrelated use case.

**Logging sensitive data in tool results.** A tool result containing a customer's SSN, a full card
number, or an internal credential, logged verbatim into a trace span or an LLM's context window
(which then persists in conversation history, provider-side logs, and possibly a fine-tuning
corpus depending on provider data policy) is a data-handling incident independent of whether
anything else in the system misbehaves. Redact at the output-validation boundary (§7.2), by field
allowlist rather than by trying to enumerate everything sensitive after the fact — an allowlist of
what's safe to forward fails safe when a new sensitive field is added upstream; a denylist does not.

**Tool sprawl with no ownership.** A registry (§5) with fifty tools and no `owner_team` field is a
registry no one will maintain — when a downstream API changes its response shape, "whose tool is
this" needs to be a lookup, not a Slack archaeology exercise, or schema drift (§4.1's result-
validation failure mode) sits undetected until a customer notices.

---

## 17. Interview questions

**1. "What's the actual mechanism behind tool calling — what does the model return, and what
executes it?"**
Weak: "the model calls the function." Strong: states precisely that the model only ever emits a
structured request (a name plus arguments); the model process never executes anything; execution
happens entirely in application code between messages 2 and 4 of the protocol dance (§1.2), and
that boundary is the basis for every safety mechanism in the rest of the system.

**2. "What's the difference between OpenAI's function calling and Anthropic's tool use, at the
wire-format level?"**
Weak: "they're basically the same." Strong: names the concrete differences (§2.1-2.2) — `parameters`
vs `input_schema`, arguments arriving as a JSON string to be parsed vs. an already-parsed dict, a
dedicated `tool` role vs. a `tool_result` content block inside a `user` message — and states why a
provider-agnostic system needs a normalization layer (§2.5) rather than branching provider-specific
code through the whole call stack.

**3. "How would you design a tool schema to maximize the chance the model calls it correctly?"**
Weak: "give it a clear name." Strong: works through §3.1's four properties in priority order —
description disambiguates against the nearest confusable tool, every parameter has a description
carrying the actual constraint, enums replace free strings wherever the value space is closed,
required/optional reflects real business necessity — and cites that this is a measurable, not
aesthetic, choice (per `20` §18 Lab 8's schema-quality experiment).

**4. "Walk me through everything that has to happen between a model deciding to call a tool and the
model seeing the result."**
Weak: "validate then execute." Strong: walks the full §4 pipeline in order — schema validation,
authorization, rate limiting, business-rule validation, idempotency check, execution, output
validation — and states why the ordering matters (every check before execution must be
side-effect-free, or a rejected call can still have changed something).

**5. "Why do you need a central tool registry instead of just importing functions into the agent
script?"**
Weak: "it's more organized." Strong: names the four concrete questions a registry answers that
scattered imports can't (§5.1) — who's authorized for which tool, whether a schema has drifted
since the last eval run, what a tool costs, whether an unreviewed high-privilege tool got added to
an agent's list — and ties dynamic per-principal scoping (§5.3) to tool-selection accuracy, not just
security.

**6. "Why is 'the LLM's output is untrusted input' the central security principle here?"**
Weak: "the model might make mistakes." Strong: states that a tool_call is structurally
indistinguishable, from a trust standpoint, from a form submission from an anonymous client —
produced by a process that can be wrong, can be steered by injected content it read, and has no
grounded channel to authorization facts — and that this is why authorization must be decided by
code against a real principal, never inferred from anything the model said, including a tool
argument that looks like an authorization fact (§6.1).

**7. "How do you implement authorization so the model can never grant itself access?"**
Weak: "check permissions before running the tool." Strong: describes the concrete mechanism (§6.1) —
authorization-relevant values (who approved this, what role applies) come exclusively from an
`ExecutionContext` populated by the application's own auth layer, never from a tool argument the
model can set, and the model's tool-facing schema doesn't even expose those fields as settable.

**8. "What's OAuth token forwarding and why does it matter for an agent acting on behalf of a
user?"**
Weak: "the agent has an API key." Strong: explains that forwarding the user's own scoped token
(§6.3) means the downstream system's own access control applies exactly as if the user called it
directly, versus a shared service-account credential silently widening every user's effective
access to whatever that account can do — a bug class invisible in testing and catastrophic in
audit.

**9. "Your agent executes a government transaction. The API times out. You don't know whether it
succeeded. What do you do?"**
Weak: "retry it" or "tell the user it failed." Strong: states plainly that both are wrong because
neither is grounded in actual knowledge of the outcome — retrying risks a duplicate submission,
assuming failure risks a duplicate through some other path later. The correct sequence (§8.4): check
status via whatever query mechanism the system exposes before doing anything else; if the system
supports client-supplied idempotency keys, a retry with the same key is safe by construction; if
neither status-check nor idempotency keys are available, escalate to a durable, explicitly
"unresolved" state for manual reconciliation rather than guessing — an honest unresolved state
surfaced to a human beats a confident wrong guess in either direction.

**10. "What's an idempotency key and how do you generate one correctly?"**
Weak: "a UUID sent with the request." Strong: states it must be generated once, deterministically,
before the first attempt of a given logical operation, and reused across every retry of that same
call (§8.3) — the most common bug being a fresh key generated inside the retry loop, which defeats
deduplication entirely — plus the `in_flight` marker needed to prevent two concurrent retries from
racing past a not-yet-completed check.

**11. "At-most-once vs. at-least-once — which do you want for a payment API call, and how do you
get it?"**
Weak: "at-most-once, obviously, we don't want double charges." Strong: explains you can't get a safe
system from choosing one delivery semantic alone — at-most-once risks silently dropping a request
that would have succeeded, at-least-once risks duplication — and that the actual fix is making the
*effect* idempotent (§8.2) so that at-least-once delivery composes safely with an exactly-once
effect, which is the industry-standard pattern (Stripe-style idempotency keys), not a delivery-layer
choice.

**12. "How do you handle a batch of parallel tool calls where one of three fails?"**
Weak: "retry the whole batch." Strong: describes returning one tool-result message per call, each
carrying its own outcome (§10.3), so the model sees exactly which two succeeded and which one
failed and can decide per-call whether to retry, proceed with partial data, or surface the gap —
never collapsing a partial failure into an all-or-nothing batch failure.

**13. "When should two tool calls not be run in parallel even if the model requested them
together?"**
Weak: "when they're related." Strong: names the actual hazard (§10.1) — not causal dependency
(which a well-formed parallel batch shouldn't have, since the model couldn't see one result before
emitting both) but a *resource conflict* invisible to the model, like two calls mutating the same
inventory SKU, which needs a declared "conflicts with" relationship per tool and a partitioning step
before dispatch, not something the model can be expected to reason about at emission time.

**14. "How do you keep a tool-calling agent from burning its whole budget retrying a dead
dependency?"**
Weak: "set a max retry count." Strong: describes a circuit breaker per tool/downstream host (§9.4)
that trips after a failure threshold and short-circuits further calls immediately with a fast,
structured failure instead of a full-timeout wait on every subsequent call — turning ten slow
timeouts into one fast failure plus nine immediate rejections.

**15. "What's the difference between a caller error and a server error in tool execution, and why
does the distinction matter for the agent's next move?"**
Weak: "one's a 4xx, one's a 5xx." Strong: states the actual behavioral consequence (§9.1) — a caller
error (bad order ID) is something the model can plausibly fix by changing its arguments and
retrying; a server error (downstream outage) is not fixable by any argument change, and telling the
model to "try again with different arguments" against a 5xx wastes a turn on an error class no
argument change addresses — the two need different structured error shapes and different retry
guidance.

**16. "How do you test a tool-calling agent without making live model API calls in CI?"**
Weak: "we test in staging against the real API." Strong: describes mocking the LLM's *decision*
with recorded or hand-constructed `tool_call` fixtures (§14.3) so the pipeline around it — schema
validation, authorization, idempotency, error handling — is under deterministic test, independent
of model sampling variance, and separately maintaining a small golden-path eval set (§14.4) that
does hit a live model, gated in CI, specifically to catch schema-quality regressions.

**17. "How does tool calling change the token cost model of a multi-turn interaction?"**
Weak: "each tool call costs some tokens." Strong: explains the compounding effect (§15.1) — every
turn resends the accumulated conversation, so total input tokens across an n-round task grow closer
to quadratically than linearly in the number of rounds, which is why a schema regression that
silently doubles the average number of tool-calling rounds can 5-8x the cost of a task, not 2x.

**18. "When would you consolidate several narrow tools into one broader tool, and when is that a
mistake?"**
Weak: "fewer tools are always better" or "more granular tools are always better." Strong: frames it
as an empirical tradeoff (§15.3) measured on tool-selection accuracy and total cost per resolved
task — consolidation wins when the operations are almost always needed together and the combined
schema stays simple; it backfires into `20` §8's schema-complexity failure mode when it produces a
tool with many optional fields serving several unrelated use cases, and the fix in that case is
splitting, not further consolidating.

**19. "How would you defend a tool-calling system against prompt injection from a retrieved
document or a tool's own result?"**
Weak: "instruct the model not to follow instructions in the data." Strong: states that an
instruction in a system prompt is not a security boundary against adversarially crafted content,
and that the actual defense lives structurally, outside the model's reasoning — every tool result
and retrieved document treated as untrusted input by default (`22` §12.4), authorization and
business-rule checks (§6, §7) enforced regardless of what a tool result or document appears to
request, and no tool argument that represents an authorization fact ever sourced from model-visible
content.

**20. "What would you audit-log for every tool call, and why include the rejected attempts?"**
Weak: "log successful calls and their results." Strong: describes the full `AuditRecord` shape
(§6.5) — principal, tenant, tool, version, redacted arguments, outcome, idempotency key, trace ID —
written on every outcome including `unauthorized` and `policy_violation`, because a rejected
attempt (a principal trying to refund an order they don't own) is frequently the more important
half of the record during a security review, and a log that only captures successes cannot answer
"did anyone try to do something they weren't allowed to."

**21. "How do you decide whether a validation failure should be surfaced to the model as
retryable?"**
Weak: "always let it retry, more chances to succeed is better." Strong: distinguishes by failure
layer (§7.4) — input validation failures are usually retry-with-feedback since the mistake is
mechanical and correctable; authorization failures are never framed as "try different arguments"
since the caller isn't allowed to do the thing regardless of arguments; business-rule failures are
retryable only when a genuinely different, valid argument set exists, and the feedback should name
that alternative explicitly rather than leaving the model to guess.

**22. "What's the single biggest mistake you've seen in a tool-calling system, and what was the
fix?"**
Weak: an anecdote with no generalizable structure. Strong: names one of §16's anti-patterns as a
structural failure, not an isolated bug — most often "authorization inferred from model output" or
"no idempotency on a state-changing tool" — explains the mechanism by which it stays invisible in a
demo and becomes expensive in production, and states the concrete architectural fix, not just "we
were more careful after that."

---

## 18. Lab exercises

**Lab 1 — Measure schema quality's effect on tool-selection and argument accuracy.**
*Goal:* turn §3.1's claim into your own measured number, per `20` §18 Lab 8's method, but scoped to
a transactional tool with real business-rule constraints (a `create_purchase_order`-style tool, not
a lookup).
*Steps:* write the same tool three ways — bare/untyped, typed with a one-line docstring, fully
specified with per-field `Field(description=...)` and enums per §3.1's four properties. Build 30-40
natural-language requests with known-correct expected arguments, run each variant, and score both
tool-selection accuracy and per-field argument accuracy.
*Artifact:* a three-row comparison table plus the three schema definitions.
*Success criterion:* a measured accuracy gap large enough to make schema quality a data-backed
requirement, not a style preference.
*Time:* ~1 day.
*Unblocks:* Labs 2 and 6.

**Lab 2 — A full §4 execution pipeline with every stage independently testable.**
*Goal:* build `execute_tool_call` (§4.2) for real, with schema validation, authorization, rate
limiting, business-rule validation, and idempotency all as separately swappable, separately unit
tested components — not inlined checks inside one function.
*Steps:* implement each stage as its own class with its own unit tests (§14.1). Wire them into the
pipeline. Write one integration test per pipeline stage that proves a failure at that stage produces
the correct `ExecutionOutcome` and never reaches execution.
*Artifact:* the pipeline implementation, unit tests per stage, and integration tests proving
short-circuit behavior at each stage.
*Success criterion:* an authorization failure and a rate-limit failure both provably never invoke
the underlying tool function — assert this with a call-count check, not by reading the code.
*Time:* ~1-2 days.
*Unblocks:* every later lab in this list.

**Lab 3 — Idempotency under induced duplicate and concurrent delivery.**
*Goal:* prove §8.3's idempotency key mechanism actually holds under both a naive retry and a race,
not just a clean single-shot call.
*Steps:* build the `IdempotencyStore` against a real datastore (Postgres or Redis, not an in-memory
dict). Write a test that issues the same logical call twice sequentially and asserts the backend
executed exactly once. Then write a test that fires the same call twice *concurrently* (real
`asyncio.gather`, not sequential awaits) and asserts the `in_flight` marker correctly serializes
them onto one execution rather than a race where both see "no result yet."
*Artifact:* the idempotency store, and both test results with the underlying execution call-count
asserted.
*Success criterion:* the concurrent test is the one that actually matters — a naive
lookup-then-execute implementation without the `in_flight` state passes the sequential test and
fails this one, which is the point of including it.
*Time:* ~half a day.
*Unblocks:* Lab 4, and any state-changing tool in a real deployment.

**Lab 4 — The status-check pattern against a fake legacy API with no idempotency support.**
*Goal:* implement §8.4's pattern for real, against a backend that deliberately does not support
client-supplied idempotency keys, forcing the status-check-before-retry design.
*Steps:* build a fake "legacy filing service" that supports submit and a natural-key status query
but nothing else, and that can be configured to drop the response on a configurable fraction of
submit calls (simulating a timeout with unknown server-side outcome). Implement
`submit_filing_with_status_check`. Run 50 submissions with a 30% induced timeout rate and confirm
zero duplicate filings and zero false "it failed" outcomes for filings that actually succeeded.
*Artifact:* the fake service, the adapter implementation, and a log of all 50 runs with outcomes.
*Success criterion:* zero duplicates and zero false negatives across the induced-timeout runs — a
single false negative here is the exact failure mode interview question §17.9 is about.
*Time:* ~1 day.
*Unblocks:* Lab 8, and any real legacy-system integration.

**Lab 5 — Parallel batch execution with a deliberate resource conflict.**
*Goal:* prove §10.1's conflict-partitioning logic actually prevents a lost-update race, not just
that it looks correct on paper.
*Steps:* implement `partition_batch` and `execute_batch` for a small set of tools including two that
declare a `CONFLICTING_RESOURCE_EXTRACTORS` entry on the same resource type. Construct a batch with
two calls that touch the *same* resource ID and confirm they land in separate sequential
sub-batches. Then remove the conflict declaration deliberately, run the same batch concurrently, and
demonstrate the lost-update race actually occurs (e.g., an inventory count that should decrement
twice only decrements once) — proving the mechanism does something, not just that it compiles.
*Artifact:* both runs' results (protected and deliberately unprotected), showing the race in the
unprotected version.
*Success criterion:* a reproduced race in the unprotected case and its absence in the protected
case, from the same test harness.
*Time:* ~half a day.
*Unblocks:* Lab 6.

**Lab 6 — A circuit breaker measured against a flaky dependency, end to end in an agent loop.**
*Goal:* connect §9.4's circuit breaker to a real agent loop (reuse `22` §17 Lab 1's loop if you've
built it) and measure the time-to-termination difference with and without the breaker.
*Steps:* wrap one tool with `ToolCircuitBreaker`, configured against a fake downstream that fails on
demand. Run the agent loop against a task requiring that tool, with the breaker disabled — measure
wall-clock time to the loop giving up. Re-run with the breaker enabled.
*Artifact:* two timing measurements and the trace from each run showing where time was spent.
*Success criterion:* a measured, not assumed, reduction in time-to-termination with the breaker
enabled — quantify how much of the difference is avoided full-timeout waits.
*Time:* ~half a day.
*Unblocks:* production readiness for any tool with an unreliable downstream.

**Lab 7 — A preview/confirm tool pair with a single-use, time-boxed token.**
*Goal:* build §11.2's preview-token pattern for real, including its dual role as both a human
approval gate and an idempotency mechanism.
*Steps:* implement `preview_purchase_order` and `create_purchase_order` with a signed,
short-expiry, single-use `preview_token` binding the exact previewed amounts to the eventual create
call. Write tests for: a valid token used once (succeeds), the same token replayed a second time
(rejected, not re-executed), an expired token (rejected with a clear reason), and a token whose
underlying preview data was tampered with (rejected — verify the signature covers the payload, not
just the token's existence).
*Artifact:* the implementation and four passing tests covering the cases above.
*Success criterion:* the replay test proves idempotency emerges from the same mechanism that
enforces the approval gate, not a separate bolt-on.
*Time:* ~1 day.
*Unblocks:* any tool needing a human-approval gate on a destructive action.

**Lab 8 — Mocked-LLM regression suite for the full tool-calling pipeline.**
*Goal:* build §14.3's deterministic test harness and use it to catch a deliberately introduced
regression without any live model call.
*Steps:* record or hand-construct a set of `NormalizedToolCall` fixtures covering: a clean
success path, a schema-validation-failure-then-retry path, an unauthorized attempt, and a
duplicate-call (idempotency) path. Wire these through `MockLLMClient` into the Lab 2 pipeline. Then
introduce a deliberate regression (loosen a validation rule, or break the idempotency key
generation to include a timestamp) and confirm the corresponding fixture test fails with a clear,
attributable assertion.
*Artifact:* the fixture set, the mock harness, and a demonstrated regression catch.
*Success criterion:* the suite runs in seconds with zero live API calls and fails specifically and
attributably on the introduced regression, not with an unrelated error.
*Time:* ~1 day.
*Unblocks:* CI gating for any tool-calling system before it reaches the P3/P4 projects in this
folder's `README.md`.

**Lab 9 — Cost-per-round measurement and one concrete reduction.**
*Goal:* turn §15.1's cost model into a measured number on a real multi-tool task, and use it to
justify one specific optimization from §15.2.
*Steps:* run a task requiring 5+ tool-calling rounds through your Lab 2 pipeline against a real
model, logging input/output tokens per round. Compute total cost and confirm the accumulation
pattern (§15.1) empirically — plot cumulative input tokens against round number. Pick one lever
(batching two calls into one richer call, or truncating an oversized tool result) and re-run,
measuring the actual cost delta.
*Artifact:* the per-round token log for both the baseline and optimized runs, and the cost
comparison.
*Success criterion:* a measured cost reduction with the specific lever named, not an estimate — and
a check that tool-selection/task-success accuracy didn't regress as the price of the savings.
*Time:* ~1 day plus API cost.
*Unblocks:* `11-token-accounting-and-cost.md`'s per-tenant attribution labs.
