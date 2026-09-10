# 07 — Generation and structured output

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow, the
> four irreducible failure classes — this chapter lives inside failure class (d): the right context
> was in the prompt, but the model produced a wrong or unusable answer),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§7 — the
> distinction between the retrieval unit and the generation unit, and why the text that enters the
> prompt is not the text that was indexed),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (§6 — the candidate
> budget and `final_k`; what arrives here is the reranker's output, and its ordering is the only
> ordering the generator will ever see),
> [`06-context-engineering.md`](06-context-engineering.md) (the entire chapter — context window
> budgeting directly determines the input to generation; §3's token arithmetic is the left-hand side
> of this chapter's cost model, and §5's assembly order determines what the model reads first),
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§10 — the generation-specific
> metrics: faithfulness, answer relevance, and the oracle-context ablation that isolates this stage
> from retrieval).
>
> **Feeds into:** [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§10–§11 — the
> structured output schemas defined here are what LLM-judge evaluators parse; §11.5's
> justification-before-verdict is a generation-side constraint),
> [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md) (a generation call is
> the most expensive span in the trace, and every retry doubles it — tracing is how you detect
> that),
> [`12-serving-latency-and-caching.md`](12-serving-latency-and-caching.md) (prompt caching and
> streaming are generation concerns that dominate the latency budget),
> [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md) (tool calling is structured
> output under a different name, and every tool-call schema in that chapter inherits this chapter's
> validation discipline),
> [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md)
> (output filtering gates are downstream consumers of the structured output contract defined here).
>
> **THESIS:** the generation step is where a data system meets a text system, and the fundamental
> tension is between the model's need for freedom — temperature, creativity, reasoning space — and
> the system's need for structure — parseable output, schema compliance, deterministic behavior.
> Structured output is not "making the LLM return JSON." It is a **contract** between the
> generation step and every downstream consumer, and like any contract it needs a schema, a
> validation layer, a retry strategy, and a degradation path. The teams that treat generation as a
> prompt-engineering exercise and structured output as a formatting convenience discover the
> contract's existence the first time a production deployment feeds a malformed answer into a
> database write, a UI render, or an agent's next tool call — and the discovery is always a
> P0 incident, never a design review.

---

## Contents

1. [The generation step in the RAG pipeline](#1-the-generation-step-in-the-rag-pipeline)
2. [Why structure matters: the contract with downstream](#2-why-structure-matters-the-contract-with-downstream)
3. [JSON mode and structured outputs — the provider landscape](#3-json-mode-and-structured-outputs--the-provider-landscape)
4. [Constrained decoding: how structured outputs work under the hood](#4-constrained-decoding-how-structured-outputs-work-under-the-hood)
5. [Schema design for LLM outputs](#5-schema-design-for-llm-outputs)
6. [Validation layers](#6-validation-layers)
7. [Retry strategies for malformed output](#7-retry-strategies-for-malformed-output)
8. [Determinism and reproducibility](#8-determinism-and-reproducibility)
9. [Citation and attribution in generated output](#9-citation-and-attribution-in-generated-output)
10. [Streaming structured output](#10-streaming-structured-output)
11. [Multi-step generation](#11-multi-step-generation)
12. [Output parsing libraries and patterns](#12-output-parsing-libraries-and-patterns)
13. [Token efficiency in structured output](#13-token-efficiency-in-structured-output)
14. [Testing structured output](#14-testing-structured-output)
15. [The cost model for generation](#15-the-cost-model-for-generation)
16. [Failure modes](#16-failure-modes)
17. [Anti-patterns](#17-anti-patterns)
18. [Mental models — the compressed set](#18-mental-models--the-compressed-set)
19. [Lab exercises](#19-lab-exercises)

---

## 1. The generation step in the RAG pipeline

```
    query
      │
      ▼
  ┌─────────────────┐
  │ query            │   05
  │ understanding    │
  └────────┬────────┘
           │  rewritten / decomposed query
           ▼
  ┌─────────────────┐
  │ retrieval +     │   03, 04
  │ reranking       │
  └────────┬────────┘
           │  ranked passages (final_k)
           ▼
  ┌─────────────────┐
  │ context         │   06
  │ assembly        │
  └────────┬────────┘
           │  assembled prompt (system + context + query)
           ▼
  ┌─────────────────────────────────────────────────┐
  │                                                 │
  │              GENERATION  (this chapter)         │
  │                                                 │
  │  input:   assembled prompt (tokens_in)          │
  │  output:  structured response (tokens_out)      │
  │  cost:    tokens_in × p_in + tokens_out × p_out │
  │  latency: TTFT + tokens_out × inter_token_lat   │
  │                                                 │
  └────────┬────────────────────────────────────────┘
           │  validated, schema-conformant output
           ▼
  ┌─────────────────┐
  │ downstream      │   API response, UI, database,
  │ consumers       │   agent loop, evaluation
  └─────────────────┘
```

### 1.1 What the generation step receives

The generation step receives a **prompt** — a token sequence assembled by the context engineering
layer (`06`). That prompt has three logical segments:

| Segment | Contents | Typical token share |
|---|---|---|
| System instructions | Persona, output schema, constraints, formatting rules | 200–2,000 |
| Retrieved context | Reranked passages, metadata, citations | 1,000–100,000 |
| User query | The original or rewritten question | 20–500 |

The ordering and proportion of these segments is `06`'s job. This chapter takes the assembled
prompt as given and concerns itself with what happens *after* the prompt is sent to the model.

### 1.2 What the generation step produces

In a production RAG system, the generation step almost never produces free text. It produces a
**structured object** — a JSON document, an XML fragment, a function-call payload — that
downstream systems can parse deterministically. Even when the user-facing output looks like prose,
the actual API response typically wraps that prose in a schema:

```python
from pydantic import BaseModel, Field

class RAGResponse(BaseModel):
    answer: str = Field(description="The answer to the user's question, grounded in the provided context.")
    citations: list[Citation] = Field(description="Source references supporting each claim in the answer.")
    confidence: float = Field(ge=0.0, le=1.0, description="Self-assessed confidence; 0.0 = no supporting evidence found.")
    reasoning: str | None = Field(default=None, description="Chain-of-thought reasoning (omitted in production, logged for debugging).")
```

The decision to make that wrapping explicit — to design the schema, validate it, and handle its
failure modes — is the difference between a demo and a production system.

### 1.3 The two failure modes unique to generation

`00` §5 defines four failure classes for the pipeline. Generation adds two sub-failures within
class (d) that deserve separate treatment:

| Sub-failure | Description | Symptom | Detection |
|---|---|---|---|
| **(d.1) Unfaithful generation** | The model produces an answer not supported by the retrieved context | Hallucination; plausible but fabricated claims | Faithfulness metric (`08` §10), citation verification (§9) |
| **(d.2) Malformed generation** | The model produces output that fails schema validation | JSON parse error, missing required fields, type violations | Schema validation (§6), runtime exceptions |

The oracle-context ablation (`00` §6, `08` §10) distinguishes class (d) from classes (a)–(c). But
it cannot distinguish (d.1) from (d.2), because a malformed response will also be "wrong." Tracing
(`10`) is what gives you that second decomposition — you need the raw model output to see whether
the answer was wrong or merely unparseable.

### 1.4 Generation is the most expensive per-query operation

For most RAG pipelines, generation dominates per-query cost. A rough breakdown on a typical
configuration (Claude Sonnet, 8k context, 500-token answer):

```
    C_query = C_embed + C_retrieve + C_rerank + C_generate

    C_embed   ≈ 0.00003  (one query embedding)
    C_retrieve ≈ 0.00000  (vector search — compute, not API cost)
    C_rerank   ≈ 0.002    (cross-encoder over ~50 passages)
    C_generate ≈ 0.03     (8,000 input tokens + 500 output tokens)

    generation share: ~93%
```

This means that every retry, every multi-step generation chain, and every schema overhead token
multiplies the dominant cost. The token-efficiency analysis in §13 and the cost model in §15 are
not optimizations — they are the first place to look when cost is too high.

---

## 2. Why structure matters: the contract with downstream

### 2.1 The consumers of generation output

In a production system, the generation step's output is consumed by at least three categories of
downstream system, each with non-negotiable expectations:

```
                          ┌──────────────────────┐
                          │  generation output    │
                          └──────────┬───────────┘
                ┌────────────────────┼────────────────────┐
                ▼                    ▼                    ▼
       ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
       │  API layer     │  │  UI renderer   │  │  Agent loop    │
       │  (serializer)  │  │  (React, etc.) │  │  (next step)   │
       └────────────────┘  └────────────────┘  └────────────────┘
       needs: typed JSON     needs: renderable   needs: parseable
       with all required     fields, no nulls    action / answer
       fields present        in display slots    to decide on
```

**The API layer** serializes the response to the client. If a required field is missing, the
serializer either throws (500 error) or silently drops it (contract violation with the client).
Both are incidents.

**The UI renderer** renders fields into components. A null `citations` array where the template
expects a list produces a runtime exception in the browser. A string where a number was expected
breaks a chart. The user sees a blank screen or an error toast.

**The agent loop** uses the response to decide its next action. If the response says
`"needs_more_context": true` but the field is missing, the agent treats it as false (or crashes),
and the wrong branch executes. This is the most dangerous consumer because failures compound
across turns.

### 2.2 Free-text output is a parse liability

The historical approach — generating free text and then regex-parsing it — fails in production for
a specific, measurable reason: **the parser's failure rate scales with output complexity**.

```python
# The tempting approach: generate text, parse it yourself
def parse_answer_from_text(raw: str) -> dict:
    """This function is a liability. Every regex is a new bug surface."""
    import re
    answer_match = re.search(r"Answer:\s*(.+?)(?:\n|$)", raw, re.DOTALL)
    confidence_match = re.search(r"Confidence:\s*(\d+\.?\d*)", raw)
    citations_raw = re.findall(r"\[(\d+)\]", raw)
    return {
        "answer": answer_match.group(1).strip() if answer_match else raw,
        "confidence": float(confidence_match.group(1)) if confidence_match else 0.5,
        "citations": [int(c) for c in citations_raw],
    }
```

This parser will produce *wrong results* without raising any error in at least four scenarios:
the model puts the confidence before the answer; the model uses "Score:" instead of "Confidence:";
the answer itself contains `[3]` as part of its text; the model outputs a numbered list where item
numbers look like citation markers. None of these are edge cases — they are the normal variance
of model output across prompt phrasings and model versions.

### 2.3 The contract formalization

A structured output contract has four components:

| Component | What it specifies | Enforcement mechanism |
|---|---|---|
| **Schema** | The shape of valid output — fields, types, constraints | JSON Schema, Pydantic model, TypeScript interface |
| **Validation** | Whether a conformant output is *semantically* correct | Custom validators, cross-field checks, range constraints |
| **Retry policy** | What to do when output fails validation | Re-prompt, output repair, fallback schema, circuit breaker |
| **Degradation path** | What to serve when all retries are exhausted | Default response, cached answer, human escalation |

Missing any one of these is a latent incident. The schema without validation accepts a response
with `confidence: 999.0`. Validation without a retry policy turns every transient failure into a
user-visible error. A retry policy without a degradation path turns persistent failures into
infinite loops or unbounded cost. The degradation path without a schema means you have no
definition of "degraded" vs. "normal."

---

## 3. JSON mode and structured outputs — the provider landscape

### 3.1 The evolution: three generations of structure enforcement

| Generation | Mechanism | Provider support | Guarantee |
|---|---|---|---|
| **Prompt-only** | "Respond in JSON format" in the system prompt | All models | None — the model *usually* complies, but can emit markdown-wrapped JSON, trailing text, or invalid syntax |
| **JSON mode** | Provider-level flag that forces syntactically valid JSON | OpenAI (`response_format: {"type": "json_object"}`), Anthropic (via tool use), Google | Valid JSON, but no schema conformance — any JSON object is accepted |
| **Structured outputs** | Provider-level schema enforcement via constrained decoding | OpenAI (2024, `response_format: {"type": "json_schema", ...}`), Anthropic (tool use with input schemas), Google (via function calling) | Schema-conformant JSON — every response matches the declared schema |

The progression matters because each generation addresses a specific failure mode the previous one
left open:

```
    prompt-only   →  JSON mode       →  structured outputs
    "might not     "always valid      "always matches
     be JSON"       JSON, might not    the declared
                    match schema"      schema"
```

### 3.2 OpenAI structured outputs

OpenAI's structured outputs (August 2024) use constrained decoding to guarantee schema conformance.
The API accepts a JSON Schema and produces output that matches it on every call.

```python
from openai import OpenAI
from pydantic import BaseModel, Field

client = OpenAI()

class Citation(BaseModel):
    chunk_id: str = Field(description="Identifier of the source chunk.")
    quote: str = Field(description="Verbatim quote from the chunk supporting the claim.")
    relevance: float = Field(ge=0.0, le=1.0, description="How directly the quote supports the claim.")

class AnswerWithCitations(BaseModel):
    answer: str = Field(description="The answer, grounded in the provided context only.")
    citations: list[Citation] = Field(description="One citation per claim in the answer.")
    confidence: float = Field(ge=0.0, le=1.0)
    unanswerable: bool = Field(description="True if the context does not contain sufficient information.")

response = client.beta.chat.completions.parse(
    model="gpt-4o-2024-08-06",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": assembled_prompt},
    ],
    response_format=AnswerWithCitations,
    temperature=0.0,
)

parsed: AnswerWithCitations = response.choices[0].message.parsed
```

**Constraints worth knowing:**

- All fields in the schema must be `required` (optional fields use a union with `null`).
- `additionalProperties` must be `false` at every level.
- Recursive schemas are supported but must use `$ref`.
- `$defs` is supported, enabling discriminated unions.
- Top-level must be an object, not an array or primitive.
- Maximum schema depth: 5 levels of nesting (as of 2025 API).

### 3.3 Anthropic structured outputs via tool use

Anthropic's Claude models produce structured output through the tool-use mechanism. You define a
"tool" whose input schema is your desired output schema, and the model "calls" the tool with
schema-conformant arguments.

```python
import anthropic
from pydantic import BaseModel, Field

client = anthropic.Anthropic()

class AnswerWithCitations(BaseModel):
    answer: str = Field(description="The answer, grounded in the provided context only.")
    citations: list[dict] = Field(description="Source references.")
    confidence: float = Field(ge=0.0, le=1.0)
    unanswerable: bool

# Define the "tool" — it won't actually be called; it's a schema declaration
answer_tool = {
    "name": "provide_answer",
    "description": "Provide a structured answer to the user's question based on the retrieved context.",
    "input_schema": AnswerWithCitations.model_json_schema(),
}

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    tools=[answer_tool],
    tool_choice={"type": "tool", "name": "provide_answer"},
    messages=[
        {"role": "user", "content": assembled_prompt},
    ],
)

# Extract the structured output from the tool-use block
for block in response.content:
    if block.type == "tool_use":
        parsed = AnswerWithCitations.model_validate(block.input)
        break
```

The `tool_choice: {"type": "tool", "name": "provide_answer"}` forces the model to use that
specific tool, which guarantees the response will contain a tool-use block with schema-conformant
input. This is functionally equivalent to OpenAI's structured outputs, implemented through a
different API surface.

### 3.4 The provider comparison table

| Feature | OpenAI structured outputs | Anthropic tool use | Google Gemini |
|---|---|---|---|
| Schema enforcement | Constrained decoding | Constrained decoding via tool schema | Function calling with schema |
| Schema language | JSON Schema (subset) | JSON Schema (via tool `input_schema`) | OpenAPI-style schema |
| Streaming | Yes, partial JSON | Yes, partial tool input | Yes |
| Refusal handling | `response.choices[0].message.refusal` field | Stop reason `end_turn` without tool use block | Finish reason check |
| Recursive schemas | Yes (via `$ref`) | Yes | Limited |
| Union types | `anyOf` supported | `anyOf` supported | Limited |
| Max schema depth | 5 levels | No hard limit documented | 5 levels |
| Caching interaction | Cached prefix includes schema | System prompt caching applies | Context caching applies |
| Token overhead | Schema tokens counted in system prompt | Tool definition tokens counted in input | Schema tokens in input |

### 3.5 When JSON mode is sufficient (and when it is not)

JSON mode — without schema enforcement — is appropriate in exactly one situation: **when the
downstream consumer can handle any valid JSON object, and you validate after the fact.** This is
rare. In practice, JSON mode without a schema is a source of silent failures: the model returns
valid JSON that is missing a field, has the wrong type for a field, or nests data differently
than expected.

The cost of structured outputs (constrained decoding) is a small increase in per-token latency and
the need to express your schema in the provider's supported subset of JSON Schema. For production
RAG systems, the tradeoff is unambiguously in favor of schema enforcement.

---

## 4. Constrained decoding: how structured outputs work under the hood

### 4.1 The decoding loop, unconstrained

A language model generates text one token at a time. At each step, the model produces a probability
distribution (logits) over the entire vocabulary:

```
    step t:
    ┌──────────────────────────────────────────────┐
    │  model(prompt + tokens_0..t-1)               │
    │  → logits: [vocab_size] floats               │
    │  → softmax → probabilities                    │
    │  → sample (or argmax at temp=0)               │
    │  → token_t                                    │
    └──────────────────────────────────────────────┘
```

Unconstrained, the model can emit any token at any position. This is why prompt-only JSON
enforcement fails: at any step, the model might produce a token that breaks JSON syntax (a missing
quote, an unescaped newline, a trailing comma before `}`).

### 4.2 Logit masking for structural constraints

Constrained decoding works by **masking logits** — setting the probability of tokens that would
violate the constraint to zero (or negative infinity before softmax) at each generation step.

```
    step t (constrained):
    ┌──────────────────────────────────────────────────┐
    │  model(prompt + tokens_0..t-1) → logits          │
    │                                                  │
    │  constraint_engine(tokens_0..t-1, schema)        │
    │  → allowed_tokens: set[int]                      │
    │                                                  │
    │  for i in range(vocab_size):                      │
    │      if i not in allowed_tokens:                  │
    │          logits[i] = -inf                         │
    │                                                  │
    │  softmax(logits) → probabilities                  │
    │  sample → token_t                                 │
    └──────────────────────────────────────────────────┘
```

The constraint engine maintains a state machine that tracks the current position within the JSON
schema. At any point, it knows which tokens are syntactically and semantically valid:

- After `{"answer": "The capital is`, only tokens that continue or close the string are allowed.
- After `"confidence":`, only tokens that begin a valid number are allowed.
- After the last field, only `}` (or `,"next_field":`) is allowed.

### 4.3 Grammar-guided generation

The most general form of constrained decoding uses a **context-free grammar (CFG)** to define the
allowed output space. JSON Schema maps naturally to a CFG:

```
    JSON Schema: {"type": "object", "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}}}

    ↓ compiles to ↓

    CFG:
    root        → '{' ws field_answer ',' ws field_score ws '}'
    field_answer→ '"answer"' ws ':' ws string
    field_score → '"score"' ws ':' ws integer
    string      → '"' char* '"'
    integer     → '-'? digit+
    char        → <any non-quote, non-backslash> | '\\' escape_char
    ws          → (' ' | '\n' | '\t')*
    ...
```

At each generation step, the engine computes the set of terminals reachable from the current
parser state and allows only tokens whose text matches one of those terminals.

### 4.4 Performance implications

Constrained decoding has measurable but usually acceptable performance costs:

| Aspect | Impact | Notes |
|---|---|---|
| Latency per token | +5–15% | Grammar state update and mask computation per step |
| Output quality | Neutral to slightly improved | Prevents wasted tokens on structural errors; the model "knows" the constraint |
| First-token latency | +10–50ms | Schema compilation and grammar construction (amortized across requests with the same schema) |
| Vocabulary utilization | Reduced | Fewer tokens compete; sampling distribution is narrower |
| Batch efficiency | Slightly reduced | Different requests in a batch may have different masks |

The key insight: **constrained decoding does not fight the model.** Well-trained models already
assign high probability to schema-conformant tokens when instructed to produce JSON. The
constraint engine eliminates the long tail of low-probability structural errors that would
otherwise require retries. The net effect is often *fewer* total tokens generated (no retries)
and *faster* end-to-end latency despite the per-token overhead.

### 4.5 The Outlines and llama.cpp approach

For self-hosted models, libraries like Outlines (Python) and llama.cpp (C++) implement
grammar-guided generation directly:

```python
# Outlines: grammar-guided generation with a local model
import outlines

model = outlines.models.transformers("meta-llama/Llama-3.1-8B-Instruct")

# From a Pydantic model
from pydantic import BaseModel

class Answer(BaseModel):
    text: str
    confidence: float
    sources: list[str]

generator = outlines.generate.json(model, Answer)
result: Answer = generator(prompt)
```

The mechanism is identical — compile the schema to a grammar, compute allowed tokens at each step,
mask the rest — but the implementation lives in the inference engine rather than behind an API.
This gives you more control (custom grammars, regex constraints, enum enforcement) at the cost of
managing the inference stack.

---

## 5. Schema design for LLM outputs

### 5.1 The schema is a steering mechanism, not just a type declaration

Field names, descriptions, and structure in a JSON Schema do double duty: they define the output
contract *and* they steer the model's generation. A field named `"answer"` with no description
produces different output than a field named `"grounded_answer"` with the description `"The answer
to the user's question, using only information from the provided context passages. If the context
does not contain sufficient information, state that explicitly."`. The schema is part of the
prompt.

This has a practical consequence: **schema design is prompt engineering with type safety.** Every
field description is an instruction. Every field name is a semantic signal. Every structural
choice (flat vs. nested, single string vs. array of claims) shapes the model's output.

### 5.2 Field descriptions as micro-prompts

```python
from pydantic import BaseModel, Field
from enum import Enum

class ConfidenceLevel(str, Enum):
    HIGH = "high"       # Multiple passages directly support the answer
    MEDIUM = "medium"   # One passage supports; others are tangentially relevant
    LOW = "low"         # No passage directly supports; answer is inferred
    NONE = "none"       # Context does not contain relevant information

class Claim(BaseModel):
    """A single factual claim extracted from the answer, with its supporting evidence."""
    statement: str = Field(
        description="One atomic factual claim. Must be verifiable against the context passages."
    )
    supporting_chunk_ids: list[str] = Field(
        description="IDs of chunks that contain evidence for this claim. Empty if the claim is unsupported."
    )
    verbatim_quote: str | None = Field(
        default=None,
        description="An exact quote from the supporting chunk. Must appear verbatim in the chunk text."
    )

class StructuredAnswer(BaseModel):
    """Complete answer with claim-level attribution."""
    summary: str = Field(
        description="A 1-3 sentence direct answer. Do not hedge unnecessarily. "
                    "If the answer is uncertain, state the uncertainty in the answer text, "
                    "not by giving a vague response."
    )
    claims: list[Claim] = Field(
        description="Decomposition of the summary into individual verifiable claims. "
                    "Every factual assertion in the summary must appear as a claim."
    )
    confidence: ConfidenceLevel = Field(
        description="Overall confidence based on how well the context supports the answer."
    )
    unanswerable_reason: str | None = Field(
        default=None,
        description="If confidence is 'none', explain what information is missing from the context. "
                    "Must be null if confidence is not 'none'."
    )
```

### 5.3 Required vs. optional fields

The treatment of optional fields differs between providers and has practical implications:

**OpenAI structured outputs** require all fields to be marked `required` in the JSON Schema. To
express optionality, you use a union type with `null`:

```python
class Answer(BaseModel):
    # For OpenAI structured outputs: use Optional / union with None
    answer: str                          # always required
    reasoning: str | None = None         # "optional" — model can set to null
    follow_up_questions: list[str] | None = None  # "optional"
```

This compiles to:
```json
{
  "type": "object",
  "properties": {
    "answer": {"type": "string"},
    "reasoning": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "follow_up_questions": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
  },
  "required": ["answer", "reasoning", "follow_up_questions"],
  "additionalProperties": false
}
```

The model *must* emit a value for every field — `null` is the "I have nothing for this" signal.
This is a feature, not a limitation: it forces the model to make an explicit decision about every
field rather than silently omitting one.

### 5.4 Nested schemas and discriminated unions

Complex RAG outputs often need different schemas for different query types. Discriminated unions
handle this cleanly:

```python
from pydantic import BaseModel, Field
from typing import Literal

class FactualAnswer(BaseModel):
    answer_type: Literal["factual"] = "factual"
    answer: str
    claims: list[Claim]
    confidence: ConfidenceLevel

class ComparisonAnswer(BaseModel):
    answer_type: Literal["comparison"] = "comparison"
    summary: str
    items_compared: list[str]
    comparison_table: list[dict[str, str]]
    winner: str | None = None

class UnansweredQuery(BaseModel):
    answer_type: Literal["unanswerable"] = "unanswerable"
    reason: str
    suggested_reformulation: str | None = None

class RAGOutput(BaseModel):
    """Discriminated union — the model chooses the answer type based on the query."""
    response: FactualAnswer | ComparisonAnswer | UnansweredQuery = Field(
        discriminator="answer_type"
    )
    retrieval_metadata: RetrievalMetadata
```

The discriminator field (`answer_type`) tells both the model and the parser which variant to
produce and expect. In constrained decoding, the grammar branches after the discriminator value
is emitted, so the model is only offered fields valid for the chosen variant.

### 5.5 Schema evolution: the migration problem

Schemas change. A new field is added, a field is renamed, an enum gains a value. In a RAG system,
schema changes interact with three things that do not change simultaneously:

1. **Cached responses** — if you cache generation output (`12`), old cached entries have the old
   schema.
2. **Client expectations** — if the API serves external clients, they parse the old schema.
3. **Evaluation datasets** — golden sets (`08` §3) contain expected outputs in the old schema.

The discipline is the same as database schema migration: **additive changes are safe; destructive
changes require a migration plan.** Add new fields as optional (nullable). Never remove a field
without a deprecation period. Never change a field's type. Version the schema in the output itself
if you anticipate frequent changes:

```python
class VersionedRAGOutput(BaseModel):
    schema_version: Literal["2.1"] = "2.1"
    response: FactualAnswer | ComparisonAnswer | UnansweredQuery
```

---

## 6. Validation layers

### 6.1 The three-layer validation stack

```
    raw model output (bytes)
          │
          ▼
    ┌─────────────────────────────┐
    │ Layer 1: Syntactic          │  "Is it valid JSON?"
    │ (JSON parse)                │  catches: truncation, invalid escapes, trailing commas
    └─────────────┬───────────────┘
                  │ valid JSON
                  ▼
    ┌─────────────────────────────┐
    │ Layer 2: Structural         │  "Does it match the schema?"
    │ (Pydantic / JSON Schema)    │  catches: missing fields, wrong types, constraint violations
    └─────────────┬───────────────┘
                  │ schema-conformant object
                  ▼
    ┌─────────────────────────────┐
    │ Layer 3: Semantic           │  "Does it make sense?"
    │ (custom validators)         │  catches: impossible values, cross-field contradictions,
    └─────────────┬───────────────┘  hallucinated references, empty citations on confident answers
                  │
                  ▼
            validated output
```

With constrained decoding (§4), Layer 1 and most of Layer 2 are guaranteed by the provider. But
Layer 3 is *never* guaranteed — no grammar can enforce that a citation ID actually exists in the
retrieved context, or that a confidence score is consistent with the number of supporting passages.

### 6.2 Structural validation with Pydantic v2

```python
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Self

class Citation(BaseModel):
    chunk_id: str
    quote: str
    relevance: float = Field(ge=0.0, le=1.0)

    @field_validator("quote")
    @classmethod
    def quote_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Citation quote must not be empty.")
        return v.strip()

class RAGResponse(BaseModel):
    answer: str
    citations: list[Citation]
    confidence: float = Field(ge=0.0, le=1.0)
    unanswerable: bool

    @field_validator("answer")
    @classmethod
    def answer_minimum_length(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError(f"Answer too short ({len(v.strip())} chars); likely a generation failure.")
        return v.strip()

    @model_validator(mode="after")
    def cross_field_consistency(self) -> Self:
        if self.unanswerable and self.confidence > 0.3:
            raise ValueError(
                f"Inconsistent: unanswerable=True but confidence={self.confidence}. "
                "Confidence should be low when the query is unanswerable."
            )
        if not self.unanswerable and not self.citations:
            raise ValueError(
                "An answerable response must include at least one citation."
            )
        return self
```

### 6.3 Semantic validation: the ground-truth check

Semantic validation answers questions that no schema can express: *Is this citation real? Does
this quote actually appear in the source? Is this confidence score plausible?*

```python
from dataclasses import dataclass

@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    metadata: dict

def validate_citations(
    response: RAGResponse,
    retrieved_chunks: list[RetrievedChunk],
) -> list[str]:
    """Validate that citations reference real chunks and quotes are verbatim.
    Returns a list of validation errors (empty = valid)."""
    errors: list[str] = []
    chunk_map = {c.chunk_id: c for c in retrieved_chunks}

    for i, citation in enumerate(response.citations):
        # Check chunk exists
        if citation.chunk_id not in chunk_map:
            errors.append(
                f"Citation {i}: chunk_id '{citation.chunk_id}' not found in retrieved context. "
                f"Available: {sorted(chunk_map.keys())}"
            )
            continue

        # Check quote is verbatim
        chunk_text = chunk_map[citation.chunk_id].text
        if citation.quote not in chunk_text:
            # Attempt fuzzy match to provide a useful error
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, citation.quote.lower(), chunk_text.lower()).ratio()
            errors.append(
                f"Citation {i}: quote not found verbatim in chunk '{citation.chunk_id}'. "
                f"Best similarity ratio: {ratio:.2f}. The model may have paraphrased."
            )

    return errors


def validate_response_semantics(
    response: RAGResponse,
    retrieved_chunks: list[RetrievedChunk],
    query: str,
) -> tuple[RAGResponse, list[str]]:
    """Full semantic validation. Returns (response, warnings).
    Raises ValueError for hard failures."""
    warnings: list[str] = []

    # 1. Citation ground-truth check
    citation_errors = validate_citations(response, retrieved_chunks)
    if citation_errors:
        # Decide: are these hard failures or warnings?
        hallucinated = [e for e in citation_errors if "not found in retrieved context" in e]
        paraphrased = [e for e in citation_errors if "not found verbatim" in e]

        if hallucinated:
            raise ValueError(
                f"Hallucinated citations detected: {hallucinated}. "
                "Response references chunks that were not in the context."
            )
        warnings.extend(paraphrased)

    # 2. Answer-citation coverage check
    if len(response.citations) == 0 and not response.unanswerable:
        raise ValueError("Response claims to answer the query but provides no citations.")

    # 3. Confidence calibration check (soft)
    if response.confidence > 0.9 and len(response.citations) < 2:
        warnings.append(
            f"High confidence ({response.confidence}) with only {len(response.citations)} citation(s). "
            "Consider whether this is well-calibrated."
        )

    return response, warnings
```

### 6.4 The validation report

In production, validation results feed into observability (`10`). Every generation call should
produce a validation report:

```python
from pydantic import BaseModel
from enum import Enum
from datetime import datetime

class ValidationSeverity(str, Enum):
    ERROR = "error"     # Hard failure — response rejected, retry or degrade
    WARNING = "warning" # Soft failure — response accepted, logged for monitoring
    INFO = "info"       # Diagnostic — no action needed

class ValidationResult(BaseModel):
    field: str
    severity: ValidationSeverity
    message: str

class GenerationValidationReport(BaseModel):
    request_id: str
    timestamp: datetime
    syntactic_valid: bool
    structural_valid: bool
    semantic_valid: bool
    results: list[ValidationResult]
    raw_output_tokens: int
    retries_needed: int
    final_status: str  # "accepted" | "degraded" | "failed"
```

The aggregation of these reports is what tells you your schema is drifting, your retry budget is
being consumed, or a model version change broke a field that used to work.

---

## 7. Retry strategies for malformed output

### 7.1 Why retries are necessary even with constrained decoding

Constrained decoding eliminates syntactic and structural failures (Layer 1 and 2 from §6.1). It
does *not* eliminate:

- **Semantic validation failures** — hallucinated citations, cross-field contradictions.
- **Refusals** — the model declines to answer, producing a schema-conformant but useless response
  (e.g., `{"answer": "I cannot answer this question.", "citations": [], "confidence": 0.0}`).
- **Truncation** — the response hits `max_tokens` before completing all fields. With constrained
  decoding, this produces a special stop reason (`length` on OpenAI, `max_tokens` on Anthropic)
  rather than invalid JSON, but the output is still incomplete.
- **Content filtering** — the provider's safety layer blocks the response.

Each requires a different retry strategy.

### 7.2 The retry decision tree

```
    generation call returns
            │
            ├── stop_reason == "content_filter"
            │   → DO NOT RETRY (same input will produce same block)
            │   → degrade: return canned safety response
            │
            ├── stop_reason == "length" (truncated)
            │   → increase max_tokens and retry (once)
            │   → if still truncated: simplify schema, reduce context
            │
            ├── syntactic/structural validation fails
            │   → should not happen with constrained decoding
            │   → if using prompt-only JSON: retry with error in prompt
            │
            ├── semantic validation fails (soft)
            │   → accept with warnings, log for monitoring
            │
            ├── semantic validation fails (hard)
            │   → retry with validation error in prompt
            │   → max 2 retries
            │
            └── output is valid
                → accept
```

### 7.3 The output-repair pattern

When the model produces a structurally valid but semantically invalid response, the most effective
retry strategy is **output repair**: include the invalid output and the validation error in the
retry prompt.

```python
import anthropic
from pydantic import ValidationError

MAX_RETRIES = 2

async def generate_with_validation(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
    tools: list[dict],
    response_model: type[BaseModel],
    retrieved_chunks: list[RetrievedChunk],
    query: str,
) -> tuple[BaseModel, int]:
    """Generate structured output with semantic validation and output repair.
    Returns (validated_response, retries_used)."""

    retries = 0
    current_messages = list(messages)

    while retries <= MAX_RETRIES:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            tools=tools,
            tool_choice={"type": "tool", "name": "provide_answer"},
            messages=current_messages,
        )

        # Extract tool use block
        tool_block = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            # Model refused to use the tool — check stop reason
            if response.stop_reason == "end_turn":
                retries += 1
                current_messages.append({"role": "assistant", "content": response.content})
                current_messages.append({
                    "role": "user",
                    "content": "You must use the provide_answer tool. Do not respond with text.",
                })
                continue
            raise GenerationError(f"Unexpected stop reason: {response.stop_reason}")

        # Structural validation
        try:
            parsed = response_model.model_validate(tool_block.input)
        except ValidationError as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise GenerationError(f"Structural validation failed after {MAX_RETRIES} retries: {e}")
            # Output repair: send the error back
            current_messages.append({"role": "assistant", "content": response.content})
            current_messages.append({
                "role": "user",
                "content": f"Your response had validation errors:\n{e}\n\nPlease fix these issues.",
            })
            continue

        # Semantic validation
        try:
            validated, warnings = validate_response_semantics(parsed, retrieved_chunks, query)
            for w in warnings:
                logger.warning("Semantic validation warning", warning=w, retries=retries)
            return validated, retries
        except ValueError as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise GenerationError(f"Semantic validation failed after {MAX_RETRIES} retries: {e}")
            current_messages.append({"role": "assistant", "content": response.content})
            current_messages.append({
                "role": "user",
                "content": (
                    f"Your response failed semantic validation:\n{e}\n\n"
                    "Please correct the response. Ensure all citations reference chunks "
                    "from the provided context and all quotes are verbatim."
                ),
            })

    raise GenerationError("Exhausted all retries.")
```

### 7.4 Fallback schemas

When the primary schema is too complex for the model to reliably populate, a **fallback schema**
provides a degraded but parseable response:

```python
class FallbackResponse(BaseModel):
    """Simplified schema used when the full schema fails validation repeatedly."""
    answer: str = Field(description="Best-effort answer to the query.")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    error_context: str = Field(
        description="Why the full structured response could not be generated."
    )

async def generate_with_fallback(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
    primary_schema: type[BaseModel],
    fallback_schema: type[BaseModel],
    **kwargs,
) -> BaseModel:
    """Try primary schema; on exhausted retries, fall back to simplified schema."""
    try:
        result, _ = await generate_with_validation(
            client, messages, primary_tools, primary_schema, **kwargs,
        )
        return result
    except GenerationError:
        # Fallback: simpler schema, fresh attempt
        logger.warning("Primary schema failed; falling back to simplified schema.")
        result, _ = await generate_with_validation(
            client, messages, fallback_tools, fallback_schema, **kwargs,
        )
        return result
```

### 7.5 Circuit breakers

When the failure rate exceeds a threshold, stop retrying and start degrading:

```python
import time
from collections import deque

class GenerationCircuitBreaker:
    """Tracks generation failure rate and trips when failures are too frequent.
    Once tripped, all requests go directly to the fallback path for a cooldown period."""

    def __init__(
        self,
        failure_threshold: float = 0.3,  # 30% failure rate trips the breaker
        window_size: int = 100,           # over the last 100 requests
        cooldown_seconds: float = 60.0,   # stay tripped for 60 seconds
    ):
        self.failure_threshold = failure_threshold
        self.window_size = window_size
        self.cooldown_seconds = cooldown_seconds
        self._results: deque[bool] = deque(maxlen=window_size)
        self._tripped_at: float | None = None

    def is_tripped(self) -> bool:
        if self._tripped_at is not None:
            if time.monotonic() - self._tripped_at < self.cooldown_seconds:
                return True
            self._tripped_at = None  # cooldown expired
        return False

    def record(self, success: bool) -> None:
        self._results.append(success)
        if not success and len(self._results) >= 10:
            failure_rate = 1 - (sum(self._results) / len(self._results))
            if failure_rate >= self.failure_threshold:
                self._tripped_at = time.monotonic()

    @property
    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        return 1 - (sum(self._results) / len(self._results))
```

### 7.6 The retry budget

Retries are not free. Each retry consumes:

- **Tokens**: the full prompt is re-sent (input tokens), plus the failed output and repair
  instruction (additional input tokens), plus a new output. On a prompt with 8,000 input tokens,
  two retries triple the input token cost.
- **Latency**: each retry adds a full round-trip. With streaming, the user sees no output during
  the retry.
- **Rate limit headroom**: retries consume the same rate limit as primary requests.

The constraint: **`max_retries × cost_per_attempt ≤ budget_per_query`**. If your budget allows
$0.05 per query and each attempt costs $0.03, you can afford one retry. Two retries put you at
$0.09 — nearly double budget on the failing queries.

This is why fallback schemas and circuit breakers exist: they bound the cost of failure.

---

## 8. Determinism and reproducibility

### 8.1 The three knobs that affect generation variability

| Parameter | Effect | When to use |
|---|---|---|
| **Temperature** | Scales logits before softmax. `temp=0` → greedy (argmax). `temp=1` → model's native distribution. `temp>1` → flatter (more random). | `temp=0` for structured output, factual QA, extraction. `temp=0.3–0.7` for conversational answers. `temp>0.7` almost never in RAG. |
| **Top-p (nucleus)** | Samples from the smallest set of tokens whose cumulative probability exceeds p. `top_p=1` → no filtering. | `top_p=0.95` is a reasonable default that clips the extreme tail without affecting common outputs. |
| **Seed** | Deterministic sampling on the server (OpenAI). With the same seed, prompt, and model, output is reproducible. | Evals, regression tests, debugging. Not a substitute for `temp=0` — it makes *sampling* deterministic, but at `temp>0` different seeds produce different output. |

### 8.2 When determinism matters

Determinism matters for exactly one purpose in production: **reproducibility of failures.** When a
user reports a bad answer and you need to reproduce it, you need the same output from the same
input. This requires:

1. Same prompt (log it — `10` §4).
2. Same model version (pin it — not "claude-sonnet-4" but "claude-sonnet-4-20250514").
3. Same temperature (log it).
4. Same seed (if supported; log it).
5. Same provider endpoint (a model deployed to different regions can produce different results due
   to hardware variance in floating-point operations).

Even with all five pinned, providers do not guarantee bitwise-identical output across API calls.
OpenAI's `seed` parameter gets close (`system_fingerprint` in the response lets you detect
infrastructure changes), but "mostly deterministic" is the practical ceiling.

### 8.3 When determinism does not matter

For most production RAG workloads, **exact reproducibility is less valuable than people assume.**
The relevant property is *consistency of quality*, not consistency of text. Two different phrasings
of the same correct answer are both acceptable. What you need is:

- A way to reproduce failures (§8.2).
- A way to measure quality variance across runs (§14, `08` §13).
- A way to detect regressions when the model or prompt changes (`08` §14).

All three are served by evaluation infrastructure, not by pinning temperature to zero on every
production request.

### 8.4 Temperature and structured output

For structured output specifically, `temperature=0` is almost always correct. The reasoning:

1. Structural tokens (`{`, `"field":`, `,`) are deterministic at any temperature — constrained
   decoding forces them.
2. Value tokens (`"The answer is..."`, `0.85`) benefit from `temp=0` because the model's highest-
   probability completion is usually the most faithful to the context.
3. Creative variation in a structured response is a bug, not a feature — if two runs of the same
   query produce different confidence scores, the score is not measuring anything.

The exception: if you want diverse candidate answers for a generate-then-select pattern (§11.2),
you need `temp>0` on the generation step and `temp=0` on the selection step.

### 8.5 Caching and determinism

Prompt caching (`12`) interacts with determinism in a non-obvious way. A cached prefix means the
model's hidden state at the cache boundary is identical across requests with the same prefix. This
makes output *more* deterministic for the cached portion, but has no effect on output variability
from the non-cached suffix.

For RAG specifically: caching the system prompt and schema definition means the only source of
output variance is the retrieved context and the query — which is exactly the variance you want.

---

## 9. Citation and attribution in generated output

### 9.1 Why citations are a system design problem, not a prompt engineering problem

In RAG, the generated answer's relationship to the retrieved context is the system's core
correctness claim. A citation is the *evidence* for that claim — it says "this part of the answer
came from this part of the context." Without citations:

- The user cannot verify the answer.
- The evaluation layer cannot measure faithfulness.
- The debugging trace cannot attribute errors to retrieval vs. generation.
- The system cannot distinguish a hallucinated claim from a grounded one.

Citations are therefore a structural component of the generation output, not an optional nicety.

### 9.2 Citation formats

| Format | Mechanism | Verifiability | User experience |
|---|---|---|---|
| **Chunk-ID reference** | `[chunk_42]` — model emits the ID of the source chunk | High if IDs are stable; fragile if chunks are re-indexed | Poor — opaque identifiers mean nothing to the user |
| **Inline verbatim quote** | Model quotes a passage from the context | Very high — quotes can be string-matched against source | Good — the user sees exactly what evidence supports the claim |
| **Document-level reference** | `[Source: annual_report_2024.pdf, p.12]` | Medium — page numbers may be approximate | Good — the user can navigate to the source |
| **Hybrid: quote + document reference** | Both a verbatim quote and a document locator | Highest — both verifiable and navigable | Best, but highest token cost |

### 9.3 Designing citations into the schema

```python
from pydantic import BaseModel, Field, model_validator
from typing import Self

class InlineCitation(BaseModel):
    """A citation that can be verified against the retrieved context."""
    chunk_id: str = Field(description="ID of the retrieved chunk this citation references.")
    document_title: str = Field(description="Human-readable title of the source document.")
    verbatim_quote: str = Field(
        description="An exact, verbatim quote from the chunk. Must appear character-for-character "
                    "in the chunk text. Do not paraphrase, summarize, or combine quotes."
    )
    page_number: int | None = Field(
        default=None,
        description="Page number in the original document, if available in chunk metadata."
    )

class CitedAnswer(BaseModel):
    """An answer where every factual claim is backed by a verifiable citation."""
    answer_text: str = Field(
        description="The answer with inline citation markers like [1], [2]. "
                    "Every factual claim must have at least one marker."
    )
    citations: list[InlineCitation] = Field(
        description="Ordered list of citations. citations[0] is [1] in the answer text."
    )

    @model_validator(mode="after")
    def check_citation_markers(self) -> Self:
        """Verify that citation markers in the answer text match the citations list."""
        import re
        markers = set(int(m) for m in re.findall(r"\[(\d+)\]", self.answer_text))
        expected = set(range(1, len(self.citations) + 1))

        # Warn on unused citations (cited but not referenced in text)
        unused = expected - markers
        if unused:
            # This is a soft warning — the citation exists but isn't referenced inline.
            # Do not fail validation; log for monitoring.
            pass

        # Fail on dangling references (referenced in text but not in citations list)
        dangling = markers - expected
        if dangling:
            raise ValueError(
                f"Answer text references citations {dangling} but only "
                f"{len(self.citations)} citations are provided."
            )
        return self
```

### 9.4 Verbatim quote verification

The strongest form of citation verification is checking that the `verbatim_quote` field actually
appears in the source chunk. This catches the most common generation failure: the model
paraphrases instead of quoting verbatim.

```python
def verify_verbatim_quotes(
    response: CitedAnswer,
    chunks: dict[str, str],  # chunk_id → chunk text
    fuzzy_threshold: float = 0.85,
) -> tuple[list[bool], list[str]]:
    """Check each citation's verbatim_quote against the source chunk.
    Returns (per_citation_pass, warnings)."""
    passes: list[bool] = []
    warnings: list[str] = []

    for i, cite in enumerate(response.citations):
        if cite.chunk_id not in chunks:
            passes.append(False)
            warnings.append(f"[{i+1}] chunk_id '{cite.chunk_id}' not in retrieved set.")
            continue

        chunk_text = chunks[cite.chunk_id]

        # Exact match (case-sensitive)
        if cite.verbatim_quote in chunk_text:
            passes.append(True)
            continue

        # Normalized match (whitespace-collapsed, case-insensitive)
        import re
        normalize = lambda s: re.sub(r"\s+", " ", s.lower().strip())
        if normalize(cite.verbatim_quote) in normalize(chunk_text):
            passes.append(True)
            warnings.append(
                f"[{i+1}] quote matched only after whitespace normalization."
            )
            continue

        # Fuzzy match
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, cite.verbatim_quote.lower(), chunk_text.lower()).ratio()
        if ratio >= fuzzy_threshold:
            passes.append(False)
            warnings.append(
                f"[{i+1}] quote not verbatim (similarity={ratio:.2f}). "
                "Model likely paraphrased."
            )
        else:
            passes.append(False)
            warnings.append(f"[{i+1}] quote not found in chunk (similarity={ratio:.2f}).")

    return passes, warnings
```

### 9.5 Citation density as a quality signal

The ratio of cited claims to total claims is a measurable signal of answer groundedness:

```
    citation_density = n_cited_claims / n_total_claims
```

A `citation_density` of 1.0 means every claim in the answer has a citation. Below 0.5, the answer
is more hallucination than grounded response. This metric feeds directly into `08` §10's
faithfulness evaluation.

### 9.6 The citation-hallucination tradeoff

Models face a tension: producing more citations increases groundedness but also increases the
chance of a fabricated citation (the model "invents" a quote that sounds right but does not
appear in the source). The schema design can steer this tradeoff:

- **Requiring `verbatim_quote`** forces the model to commit to a specific string that can be
  verified. This reduces fabricated citations because the model "knows" the quote will be checked.
- **Limiting citation count** (`max_items` on the citations array) prevents the model from
  padding with low-quality citations.
- **Making citations optional** on low-confidence answers avoids forcing the model to fabricate
  evidence when the context is thin.

---

## 10. Streaming structured output

### 10.1 The latency problem

Generation latency has two components:

```
    total_latency = TTFT + (n_output_tokens × inter_token_latency)

    TTFT:   time to first token — dominated by prompt processing
    ITL:    inter-token latency — relatively constant per model

    Example (Claude Sonnet, 8k context, 500 output tokens):
    TTFT ≈ 0.5–1.5s
    ITL  ≈ 20–40ms/token
    total ≈ 1.5 + 500 × 0.03 ≈ 16.5s
```

Without streaming, the user waits 16.5 seconds for any output. With streaming, the user sees
the first token after 1.5 seconds and watches the response build incrementally. This is not a
performance optimization — the same total work is done — it is a **perceived latency optimization**
that fundamentally changes the user experience.

### 10.2 Streaming structured output: the partial-parse problem

Streaming a free-text response is straightforward: each token is appended and displayed. Streaming
a structured JSON response is not, because JSON is not incrementally parseable in the naive sense
— `{"answer": "The capital` is not valid JSON.

The solution is **partial JSON parsing**: a parser that can extract completed fields from an
incomplete JSON stream.

```python
import json
from typing import Any

class PartialJSONParser:
    """Incrementally parse a JSON stream, yielding completed fields as they appear.

    This is a simplified version of the partial parsing that libraries like
    partial-json-parser and instructor's streaming mode implement.
    """

    def __init__(self):
        self._buffer = ""
        self._completed_fields: dict[str, Any] = {}

    def feed(self, chunk: str) -> dict[str, Any]:
        """Feed a new chunk from the stream. Returns any newly completed fields."""
        self._buffer += chunk
        new_fields: dict[str, Any] = {}

        # Attempt to parse completed fields by trying valid JSON completions
        # Strategy: for each potential field boundary, try closing the JSON
        try:
            # Try parsing the buffer as-is (works when stream is complete)
            parsed = json.loads(self._buffer)
            for k, v in parsed.items():
                if k not in self._completed_fields:
                    new_fields[k] = v
                    self._completed_fields[k] = v
            return new_fields
        except json.JSONDecodeError:
            pass

        # Try closing open strings and objects to extract completed fields
        for close_suffix in ['"}', '"}]', '"}]}', '"}'']:
            try:
                parsed = json.loads(self._buffer + close_suffix)
                for k, v in parsed.items():
                    if k not in self._completed_fields and isinstance(v, (str, int, float, bool)):
                        if self._is_field_complete(k):
                            new_fields[k] = v
                            self._completed_fields[k] = v
            except (json.JSONDecodeError, Exception):
                continue

        return new_fields

    def _is_field_complete(self, field_name: str) -> bool:
        """Heuristic: a field is complete if we've seen its value followed by a comma or closing brace."""
        # Look for the pattern: "field_name": value, or "field_name": value}
        import re
        pattern = rf'"{re.escape(field_name)}"\s*:\s*(?:"[^"]*"|[\d.]+|true|false|null)\s*[,}}]'
        return bool(re.search(pattern, self._buffer))
```

### 10.3 Provider streaming APIs

Both OpenAI and Anthropic support streaming structured output:

```python
# Anthropic streaming with tool use
import anthropic

client = anthropic.Anthropic()

with client.messages.stream(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    tools=[answer_tool],
    tool_choice={"type": "tool", "name": "provide_answer"},
    messages=messages,
) as stream:
    for event in stream:
        if event.type == "content_block_delta":
            if hasattr(event.delta, "partial_json"):
                # event.delta.partial_json contains the incremental JSON text
                partial = event.delta.partial_json
                # Feed to partial parser or accumulate for final parse
                handle_partial_json(partial)
        elif event.type == "content_block_stop":
            # Tool use block is complete; parse the full input
            pass

    # After stream completes, get the final message
    final_message = stream.get_final_message()
```

### 10.4 Streaming to the UI: Server-Sent Events

The standard pattern for streaming generation output to a web UI is Server-Sent Events (SSE):

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

@app.post("/api/chat")
async def chat(request: ChatRequest):
    async def event_stream():
        async for chunk in generate_streaming(request):
            # Each chunk is a partial update
            if chunk.type == "text_delta":
                yield f"data: {json.dumps({'type': 'text', 'content': chunk.text})}\n\n"
            elif chunk.type == "field_complete":
                yield f"data: {json.dumps({'type': 'field', 'name': chunk.field, 'value': chunk.value})}\n\n"
            elif chunk.type == "done":
                yield f"data: {json.dumps({'type': 'done', 'full_response': chunk.response.model_dump()})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 10.5 Streaming and validation: the timing question

Validation cannot run until the output is complete. This creates a tension: the user has been
watching tokens stream for 15 seconds, and then validation fails and the system starts a retry.
The user sees the response vanish.

Three mitigation strategies:

1. **Optimistic display, post-hoc validation.** Show the streamed output as it arrives. If
   validation fails, show the degraded response with a warning rather than replacing the answer.
   The user has already read most of the response and may find it acceptable.

2. **Field-level streaming with early validation.** Validate individual fields as they complete.
   If the `confidence` field is `0.95` but the `citations` array is empty, you know a cross-field
   violation is coming and can start a retry before the full response completes.

3. **Dual-stream: text and metadata.** Stream the `answer` field directly to the UI (the part the
   user reads). Validate the structured fields (citations, confidence) silently in the background.
   The user experience is uninterrupted; the system logs any validation warnings.

---

## 11. Multi-step generation

### 11.1 Why single-shot generation is often insufficient

Single-shot generation — one prompt, one response — works well for simple factual queries. It
fails for:

- **Complex reasoning** requiring intermediate steps the model must work through.
- **Long answers** where the model must maintain consistency across many paragraphs.
- **High-fidelity extraction** where the model must first understand the context and then
  structure its understanding.
- **Self-correction** where the first answer needs refinement.

Multi-step generation decomposes these into a sequence of generation calls, each with its own
prompt and schema.

### 11.2 The think-then-extract pattern

The most common multi-step pattern separates reasoning from output:

```
    Step 1 (think): unstructured reasoning over the context
    Step 2 (extract): structured output from the reasoning

    ┌───────────────────────────────────────┐
    │ Step 1: Chain-of-thought              │
    │ input: context + query                │
    │ output: free-text reasoning           │
    │ temp: 0.0                             │
    │ schema: none (or minimal)             │
    └────────────┬──────────────────────────┘
                 │ reasoning text
                 ▼
    ┌───────────────────────────────────────┐
    │ Step 2: Structured extraction         │
    │ input: reasoning + query              │
    │ output: validated schema              │
    │ temp: 0.0                             │
    │ schema: full AnswerWithCitations      │
    └───────────────────────────────────────┘
```

```python
async def think_then_extract(
    client: anthropic.AsyncAnthropic,
    context: str,
    query: str,
) -> AnswerWithCitations:
    """Two-step generation: reason freely, then extract structured output."""

    # Step 1: Think
    thinking_response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": (
                f"Context:\n{context}\n\n"
                f"Question: {query}\n\n"
                "Think through this step by step. Identify which passages are relevant, "
                "what they say, and how confident you are in the answer. "
                "Note any gaps in the evidence."
            ),
        }],
        temperature=0.0,
    )
    reasoning = thinking_response.content[0].text

    # Step 2: Extract
    extraction_response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        tools=[answer_tool],
        tool_choice={"type": "tool", "name": "provide_answer"},
        messages=[{
            "role": "user",
            "content": (
                f"Based on this analysis:\n{reasoning}\n\n"
                f"Original question: {query}\n\n"
                "Now provide your structured answer using the provide_answer tool."
            ),
        }],
        temperature=0.0,
    )

    tool_block = next(b for b in extraction_response.content if b.type == "tool_use")
    return AnswerWithCitations.model_validate(tool_block.input)
```

The cost is two generation calls — roughly 2x the token cost. The benefit is measurably better
quality on complex queries, because the reasoning step gives the model space to work through
the evidence before committing to a structured answer. `08` §10 describes how to measure whether
this tradeoff pays.

### 11.3 Extended thinking (native chain-of-thought)

Several providers now support extended thinking as a native model feature, where the model reasons
internally before producing output. This is functionally the think-then-extract pattern implemented
inside a single API call:

```python
# Anthropic extended thinking
response = await client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=16384,
    thinking={
        "type": "enabled",
        "budget_tokens": 8192,  # tokens allocated for internal reasoning
    },
    tools=[answer_tool],
    tool_choice={"type": "tool", "name": "provide_answer"},
    messages=[{
        "role": "user",
        "content": assembled_prompt,
    }],
)

# The response contains thinking blocks (not visible to the user) and tool use
for block in response.content:
    if block.type == "thinking":
        # Internal reasoning — log for debugging, do not surface
        logger.debug("Model reasoning", thinking=block.thinking)
    elif block.type == "tool_use":
        parsed = AnswerWithCitations.model_validate(block.input)
```

The advantage over the manual two-step: one API call, one round-trip, and the model's reasoning
is directly connected to its structured output (no information loss in the handoff).

### 11.4 Generate-then-validate-then-refine

A self-critique loop where the model evaluates its own output:

```python
async def generate_with_self_critique(
    client: anthropic.AsyncAnthropic,
    context: str,
    query: str,
    max_refinements: int = 1,
) -> AnswerWithCitations:
    """Generate, self-critique, refine. Bounded to max_refinements iterations."""

    answer = await generate_structured_answer(client, context, query)

    for i in range(max_refinements):
        # Self-critique step
        critique_response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    f"Context:\n{context}\n\n"
                    f"Question: {query}\n\n"
                    f"Proposed answer:\n{answer.model_dump_json(indent=2)}\n\n"
                    "Evaluate this answer critically:\n"
                    "1. Does every citation quote actually appear verbatim in the context?\n"
                    "2. Does the answer make claims not supported by the cited evidence?\n"
                    "3. Is the confidence level appropriate given the evidence?\n"
                    "4. Are there relevant passages in the context that the answer ignores?\n\n"
                    "If the answer is satisfactory, respond with 'APPROVED'.\n"
                    "Otherwise, describe the specific issues."
                ),
            }],
            temperature=0.0,
        )

        critique = critique_response.content[0].text
        if "APPROVED" in critique.upper():
            break

        # Refinement step
        answer = await generate_structured_answer(
            client, context, query,
            additional_instruction=(
                f"A review of your previous answer found these issues:\n{critique}\n\n"
                "Please correct them in your new response."
            ),
        )

    return answer
```

**Cost:** up to `3 × max_refinements + 1` generation calls. **Use sparingly** — the marginal
quality improvement decreases rapidly, and the cost increases linearly. Measure the improvement
on your eval set (`08` §10) before enabling in production.

### 11.5 Multi-step generation and latency

Each step adds a full round-trip:

```
    single-shot:   TTFT + n × ITL                      ≈ 15s
    think-extract: 2 × (TTFT + n × ITL)                ≈ 25s
    self-critique: 3 × (TTFT + n × ITL)                ≈ 40s
```

For synchronous request-response APIs, this is often unacceptable. For async workflows (batch
processing, background enrichment, agent loops), it is the right tradeoff. The decision is product-
driven, not engineering-driven.

---

## 12. Output parsing libraries and patterns

### 12.1 The library landscape

| Library | Approach | Strengths | Limitations |
|---|---|---|---|
| **Instructor** | Pydantic-first; patches provider SDKs to add structured output | Clean Pydantic integration, automatic retries, multi-provider, streaming | Tight coupling to provider SDKs; retry logic can conflict with your own |
| **Outlines** | Grammar-guided generation for local models | True constrained decoding at the inference level; regex and CFG support | Only works with local models (Transformers, vLLM, llama.cpp) |
| **Guidance** | Template language with constrained generation | Fine-grained control over generation; interleaves text and constraints | Complex API; limited provider support |
| **Marvin** | Lightweight extraction with Pydantic | Simple API for extraction tasks | Less flexible for complex generation patterns |
| **LangChain OutputParsers** | String-based parsing with Pydantic validation | Part of the LangChain ecosystem; many built-in parsers | String-based parsing is fragile (§2.2); the entire `20` chapter applies |
| **LiteLLM** | Unified API across providers with structured output | Provider abstraction; handles JSON mode differences | Abstraction can hide provider-specific behaviors |

### 12.2 Instructor in depth

Instructor is the most widely adopted library for structured output in Python and warrants detailed
examination:

```python
import instructor
import anthropic
from pydantic import BaseModel, Field

# Patch the Anthropic client
client = instructor.from_anthropic(anthropic.Anthropic())

class ResearchAnswer(BaseModel):
    """Structured answer for a research question."""
    summary: str = Field(description="2-3 sentence summary answering the question.")
    key_findings: list[str] = Field(
        description="Bullet points of key findings from the context.",
        min_length=1,
        max_length=10,
    )
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str] = Field(
        description="Known limitations or gaps in the available evidence."
    )

# Generate structured output with automatic retries
response = client.chat.completions.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    max_retries=2,  # Instructor handles retry logic
    messages=[
        {"role": "user", "content": assembled_prompt},
    ],
    response_model=ResearchAnswer,
)

# response is already a validated ResearchAnswer instance
assert isinstance(response, ResearchAnswer)
```

**What Instructor does under the hood:**

1. Converts the Pydantic model to a tool/function schema.
2. Sends the request with tool/function calling.
3. Parses the response into the Pydantic model.
4. If validation fails, retries with the validation error included in the prompt.
5. Returns the validated Pydantic instance.

**When to use Instructor vs. rolling your own:**

- **Use Instructor** when: your schema is stable, you want Pydantic integration without
  boilerplate, and the built-in retry logic matches your requirements.
- **Roll your own** when: you need custom retry logic (§7), circuit breakers (§7.5), semantic
  validation (§6.3), or fine-grained control over the prompt during retries.

### 12.3 Outlines for self-hosted models

```python
import outlines
from pydantic import BaseModel, Field
from enum import Enum

class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"

class SentimentAnalysis(BaseModel):
    sentiment: Sentiment
    confidence: float = Field(ge=0.0, le=1.0)
    key_phrases: list[str]

model = outlines.models.transformers("meta-llama/Llama-3.1-8B-Instruct")
generator = outlines.generate.json(model, SentimentAnalysis)

result: SentimentAnalysis = generator(prompt)
```

Outlines implements constrained decoding at the token level — the same mechanism providers use
(§4.2), but running in your inference stack. This gives you:

- **Zero retry rate** for structural conformance — every output matches the schema.
- **Custom regex constraints** — enforce patterns like email addresses, dates, or identifiers
  that JSON Schema cannot express.
- **Grammar composition** — combine multiple constraints in a single generation.

The tradeoff: you manage the model infrastructure. For teams already running self-hosted models,
this is usually the right choice.

### 12.4 The build-vs-buy decision

```
    Decision: should I use a library or build my own structured output layer?

    ┌─ Schema complexity: low (< 5 fields, flat)
    │   └─ Use Instructor or provider's built-in. Not worth custom code.
    │
    ├─ Schema complexity: medium (nested, unions, 10-20 fields)
    │   ├─ Retry logic is standard → Instructor
    │   └─ Retry logic is custom (circuit breakers, semantic validation) → build on top of provider SDK
    │
    └─ Schema complexity: high (multi-step, conditional schemas, complex validation)
        └─ Build your own. The library will fight you.
```

---

## 13. Token efficiency in structured output

### 13.1 The overhead problem

Structured output adds tokens in three places:

```
    ┌───────────────────────────────────────────────────────┐
    │                  INPUT TOKENS                         │
    │                                                       │
    │  1. Schema definition (tool/function definition)      │
    │     → 200–1,500 tokens depending on schema complexity │
    │                                                       │
    │  2. Schema instructions in system prompt              │
    │     → 100–500 tokens                                  │
    │                                                       │
    │  3. Field descriptions (part of schema)               │
    │     → 50–300 tokens                                   │
    │                                                       │
    │  TOTAL INPUT OVERHEAD: 350–2,300 tokens               │
    └───────────────────────────────────────────────────────┘

    ┌───────────────────────────────────────────────────────┐
    │                  OUTPUT TOKENS                         │
    │                                                       │
    │  4. Structural tokens ({, }, ",  "field_name":, etc.) │
    │     → 20–40% of output tokens                        │
    │                                                       │
    │  5. Repetitive field names in arrays                  │
    │     → variable, significant for large arrays           │
    │                                                       │
    │  TOTAL OUTPUT OVERHEAD: 20–40% of output tokens       │
    └───────────────────────────────────────────────────────┘
```

### 13.2 Quantifying the overhead

Consider a concrete example — an answer with 3 citations:

```
    Free-text answer (estimated):
    "The capital of France is Paris. This is stated in Source A (page 12)
     and confirmed in Source B (page 3). The city has been the capital
     since the 10th century according to Source C."
    → ~50 tokens

    Structured equivalent:
    {
      "answer": "The capital of France is Paris. The city has been the capital since the 10th century.",
      "citations": [
        {"chunk_id": "doc_a_chunk_12", "quote": "Paris is the capital of France", "page": 12},
        {"chunk_id": "doc_b_chunk_3", "quote": "Paris, the French capital", "page": 3},
        {"chunk_id": "doc_c_chunk_7", "quote": "capital since the 10th century", "page": null}
      ],
      "confidence": 0.95,
      "unanswerable": false
    }
    → ~120 tokens (structural overhead: ~70 tokens)
```

The structured version is 2.4x the free-text tokens. At $15/M output tokens (Claude Sonnet), the
difference is $0.001 per response — negligible. But at scale (1M queries/month), that is $1,050
extra per month. And for more complex schemas with larger arrays, the overhead compounds.

### 13.3 Optimization strategies

| Strategy | Savings | Tradeoff |
|---|---|---|
| **Short field names** | 10–20% on output structural tokens | Readability; schema is harder to understand; field names lose their steering effect |
| **Flatten nested objects** | 5–15% on structural tokens | Schema is less logically organized |
| **Limit array sizes** | Variable, can be large | Potential information loss |
| **Compress field descriptions** | 5–10% on input tokens | Weaker steering of model output |
| **Cache the schema prefix** | Up to 90% on schema input tokens across requests | Only works with prompt caching (`12`) |

**The recommendation:** optimize schema input tokens via caching first — it is free quality. Then
optimize output tokens only if the cost analysis in §15 says generation cost is your bottleneck.
Do *not* sacrifice field-description quality for token savings — the steering effect (§5.1) of
good descriptions almost always outweighs the cost.

### 13.4 Field name optimization: when it matters

```python
# Verbose but well-steered (recommended for most cases)
class DetailedCitation(BaseModel):
    source_chunk_identifier: str = Field(description="...")
    verbatim_quote_from_source: str = Field(description="...")
    relevance_score: float = Field(description="...")

# Compact (only when output volume justifies it)
class CompactCitation(BaseModel):
    id: str
    q: str
    r: float
```

The compact version saves ~15 output tokens per citation. Over an array of 5 citations, that is
75 tokens — about $0.001 at Claude Sonnet rates. Unless you are generating millions of responses,
the verbose version is worth its cost because the field names *steer the model*: `verbatim_quote_from_source` produces more accurate quotes than `q`.

### 13.5 Prompt caching for schema overhead

The schema definition is identical across requests. With prompt caching (`12`), the schema tokens
are processed once and reused:

```python
# Anthropic prompt caching: mark the system prompt (including tool definitions)
# as cacheable. The schema tokens are paid at full price on the first request
# and at ~10% on subsequent requests within the cache window.

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    system=[
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ],
    tools=[answer_tool],  # schema definition — cached as part of the system turn
    tool_choice={"type": "tool", "name": "provide_answer"},
    messages=messages,
)
```

At a 90% cache hit rate, a 1,500-token schema costs 150 effective tokens per request instead of
1,500. This is the single most impactful token-efficiency optimization for structured output.

---

## 14. Testing structured output

### 14.1 The testing problem

Structured output is harder to test than most software because the system under test is
nondeterministic. The same input can produce different valid outputs, and "valid" has three
layers (§6.1). A test suite must cover:

| What to test | Testing method | Stability |
|---|---|---|
| Schema conformance | Unit tests against the Pydantic model | Deterministic |
| Validator behavior | Unit tests with crafted invalid inputs | Deterministic |
| Model output quality | Eval suite with golden answers | Nondeterministic |
| Retry and degradation | Integration tests with mocked failures | Deterministic |
| End-to-end contract | Contract tests against the API schema | Deterministic |

### 14.2 Property-based testing for schemas

Property-based testing generates random valid instances of your schema and checks invariants:

```python
from hypothesis import given, strategies as st, settings
from hypothesis_jsonschema import from_schema
import json

# Generate random valid instances of the schema
schema = AnswerWithCitations.model_json_schema()

@given(data=from_schema(schema))
@settings(max_examples=200)
def test_schema_roundtrip(data: dict):
    """Any valid JSON matching the schema should parse and re-serialize identically."""
    parsed = AnswerWithCitations.model_validate(data)
    reserialized = json.loads(parsed.model_dump_json())
    reparsed = AnswerWithCitations.model_validate(reserialized)
    assert parsed == reparsed

@given(data=from_schema(schema))
@settings(max_examples=200)
def test_validators_do_not_crash(data: dict):
    """Validators should raise ValidationError, never an unhandled exception."""
    from pydantic import ValidationError
    try:
        AnswerWithCitations.model_validate(data)
    except ValidationError:
        pass  # Expected for some random inputs
    # Any other exception is a bug in the validator
```

### 14.3 Validator unit tests

```python
import pytest
from pydantic import ValidationError

class TestRAGResponseValidation:
    """Deterministic tests for the validation layer."""

    def test_valid_response_passes(self):
        response = RAGResponse(
            answer="The capital of France is Paris.",
            citations=[Citation(chunk_id="c1", quote="Paris is the capital", relevance=0.9)],
            confidence=0.95,
            unanswerable=False,
        )
        assert response.confidence == 0.95

    def test_empty_answer_rejected(self):
        with pytest.raises(ValidationError, match="Answer too short"):
            RAGResponse(
                answer="   ",
                citations=[],
                confidence=0.0,
                unanswerable=True,
            )

    def test_unanswerable_with_high_confidence_rejected(self):
        with pytest.raises(ValidationError, match="Inconsistent"):
            RAGResponse(
                answer="I cannot answer this question based on the provided context.",
                citations=[],
                confidence=0.9,
                unanswerable=True,
            )

    def test_answerable_without_citations_rejected(self):
        with pytest.raises(ValidationError, match="at least one citation"):
            RAGResponse(
                answer="The capital of France is Paris.",
                citations=[],
                confidence=0.8,
                unanswerable=False,
            )

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            RAGResponse(
                answer="The capital of France is Paris.",
                citations=[Citation(chunk_id="c1", quote="Paris is the capital", relevance=0.9)],
                confidence=1.5,
                unanswerable=False,
            )
```

### 14.4 Snapshot testing for prompts

Prompt changes can silently break structured output. Snapshot tests detect unintended changes:

```python
import hashlib

def test_system_prompt_unchanged():
    """Detect unintended prompt changes that might affect output format."""
    prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    # Update this hash deliberately when the prompt changes
    assert prompt_hash == "a1b2c3d4e5f6...", (
        "System prompt has changed. If this is intentional, update the hash "
        "and run the regression eval suite to verify output quality."
    )

def test_schema_unchanged():
    """Detect schema changes that might break downstream consumers."""
    schema_json = AnswerWithCitations.model_json_schema()
    schema_hash = hashlib.sha256(
        json.dumps(schema_json, sort_keys=True).encode()
    ).hexdigest()
    assert schema_hash == "f6e5d4c3b2a1...", (
        "Schema has changed. If this is intentional, update the hash, "
        "run the regression eval, and check client compatibility."
    )
```

### 14.5 Integration tests with mocked failures

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_retry_on_semantic_validation_failure():
    """Verify that semantic validation failures trigger a retry with error context."""
    mock_client = AsyncMock()

    # First call: valid structure but hallucinated citation
    first_response = make_mock_response(
        tool_input={"answer": "Paris is the capital.", "citations": [
            {"chunk_id": "nonexistent_chunk", "quote": "fake quote", "relevance": 0.9}
        ], "confidence": 0.9, "unanswerable": False}
    )
    # Second call: corrected citations
    second_response = make_mock_response(
        tool_input={"answer": "Paris is the capital.", "citations": [
            {"chunk_id": "real_chunk_1", "quote": "Paris is the capital of France", "relevance": 0.9}
        ], "confidence": 0.9, "unanswerable": False}
    )
    mock_client.messages.create = AsyncMock(side_effect=[first_response, second_response])

    result, retries = await generate_with_validation(
        mock_client, messages, tools, RAGResponse,
        retrieved_chunks=[RetrievedChunk("real_chunk_1", "Paris is the capital of France", {})],
        query="What is the capital of France?",
    )

    assert retries == 1
    assert result.citations[0].chunk_id == "real_chunk_1"
    assert mock_client.messages.create.call_count == 2

    # Verify the retry prompt included the validation error
    retry_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
    assert "Hallucinated citations" in retry_messages[-1]["content"]
```

### 14.6 Regression testing for model version changes

Model version changes are the most common cause of structured output breakage. A regression suite
runs the same inputs against the new model and compares output quality:

```python
def run_structured_output_regression(
    model_a: str,  # e.g., "claude-sonnet-4-20250514"
    model_b: str,  # e.g., "claude-sonnet-4-20250715"
    test_cases: list[dict],
    schema: type[BaseModel],
) -> dict:
    """Compare structured output quality across model versions.
    Returns per-field agreement rates and validation pass rates."""
    results = {"model_a_valid": 0, "model_b_valid": 0, "both_valid": 0, "total": len(test_cases)}

    for case in test_cases:
        a_output = generate(model_a, case["prompt"], schema)
        b_output = generate(model_b, case["prompt"], schema)

        a_valid = validate(a_output)
        b_valid = validate(b_output)

        results["model_a_valid"] += a_valid
        results["model_b_valid"] += b_valid
        results["both_valid"] += (a_valid and b_valid)

    results["a_pass_rate"] = results["model_a_valid"] / results["total"]
    results["b_pass_rate"] = results["model_b_valid"] / results["total"]
    results["regression"] = results["b_pass_rate"] < results["a_pass_rate"] - 0.02

    return results
```

---

## 15. The cost model for generation

### 15.1 The fundamental equation

```
    C_generation = C_input + C_output + C_overhead

    C_input    = tokens_in × price_per_input_token
    C_output   = tokens_out × price_per_output_token
    C_overhead = C_retries + C_multi_step + C_validation

    where:
    tokens_in  = tokens_system + tokens_schema + tokens_context + tokens_query
    tokens_out = tokens_structural + tokens_content
    C_retries  = expected_retries × (C_input + C_output + C_repair_tokens)
```

### 15.2 Current pricing landscape (mid-2025, verify before using)

| Model | Input ($/M tokens) | Output ($/M tokens) | Cached input ($/M) | Notes |
|---|---|---|---|---|
| Claude Sonnet 4 | $3 | $15 | $0.30 | Best quality/cost for structured output |
| Claude Haiku 3.5 | $0.80 | $4 | $0.08 | Good for simpler schemas, extraction |
| GPT-4o | $2.50 | $10 | $1.25 | Native structured outputs API |
| GPT-4o-mini | $0.15 | $0.60 | $0.075 | Cheapest option; may struggle with complex schemas |
| Claude Opus 4 | $15 | $75 | $1.50 | For complex reasoning; expensive for routine generation |

**These prices change.** Do not design a cost model around specific numbers. Design it around
the *structure*: input tokens are cheap relative to output tokens (3–5x), caching makes input
nearly free (10–20x reduction), and the model tier choice is a 10–20x lever.

### 15.3 Worked example

A RAG system answering 100,000 queries/month with Claude Sonnet:

```
    Per query:
    tokens_in:  8,000 (context) + 500 (system) + 1,000 (schema) + 100 (query) = 9,600
    tokens_out: 500 (answer + citations + metadata)
    retries:    5% of queries need 1 retry

    Without caching:
    C_input    = 9,600 × $3/M    = $0.0288
    C_output   = 500 × $15/M     = $0.0075
    C_retries  = 0.05 × ($0.0288 + $0.0075 + $0.005) = $0.0021
    C_per_query = $0.0384

    Monthly: 100,000 × $0.0384 = $3,840

    With caching (system + schema = 1,500 tokens cached at 90% hit rate):
    C_input    = (8,100 × $3/M) + (1,500 × 0.1 × $3/M) + (1,500 × 0.9 × $0.30/M)
               = $0.0243 + $0.00045 + $0.000405 = $0.02516
    C_per_query = $0.02516 + $0.0075 + $0.0021 = $0.0348

    Monthly: 100,000 × $0.0348 = $3,476
    Savings: $364/month (9.5%) — modest because context dominates.
```

The insight: **context tokens dominate input cost.** Caching the schema saves money, but reducing
context tokens (`06` §3) saves more. And output tokens cost 5x more per token, so every
unnecessary structural token in the output is 5x more expensive than a context token.

### 15.4 Model selection by task

Not every generation call needs the same model. In a multi-step pipeline (§11), you can use
different models for different steps:

```python
MODEL_CONFIG = {
    "reasoning": {
        "model": "claude-sonnet-4-20250514",      # Quality matters for reasoning
        "max_tokens": 4096,
        "temperature": 0.0,
    },
    "extraction": {
        "model": "claude-haiku-3-5-20241022",     # Structured extraction is simpler
        "max_tokens": 2048,
        "temperature": 0.0,
    },
    "classification": {
        "model": "claude-haiku-3-5-20241022",     # Enum classification is cheap
        "max_tokens": 256,
        "temperature": 0.0,
    },
    "self_critique": {
        "model": "claude-sonnet-4-20250514",      # Critique needs quality
        "max_tokens": 1024,
        "temperature": 0.0,
    },
}
```

The rule: **use the cheapest model that reliably passes your validation layer.** "Reliably"
means < 5% retry rate on your eval set. If the cheap model needs 20% retries, the retries cost
more than using the better model.

### 15.5 Batching

For offline workloads (evaluation, bulk enrichment, migration), batching reduces cost by
50% on most providers:

```python
# Anthropic Message Batches API
import anthropic

client = anthropic.Anthropic()

# Create batch
batch = client.messages.batches.create(
    requests=[
        {
            "custom_id": f"query_{i}",
            "params": {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "tools": [answer_tool],
                "tool_choice": {"type": "tool", "name": "provide_answer"},
                "messages": [{"role": "user", "content": prompt}],
            },
        }
        for i, prompt in enumerate(prompts)
    ]
)

# Poll for completion (or use webhook)
# Results arrive within 24 hours at 50% discount
```

Batch pricing is the strongest argument for separating online (user-facing) and offline
(evaluation, enrichment) generation workloads: the same model, the same quality, half the cost,
with the tradeoff being latency (hours instead of seconds).

---

## 16. Failure modes

### 16.1 Partial outputs

The model hits `max_tokens` before completing the structured output. With constrained decoding,
the provider signals this via the stop reason (`length` or `max_tokens`) rather than producing
invalid JSON. Without constrained decoding, the output is truncated mid-JSON and unparseable.

**Detection:** check the stop reason on every response.

```python
def check_stop_reason(response) -> None:
    """Raise on truncation before downstream parsing attempts."""
    if hasattr(response, "stop_reason"):
        # Anthropic
        if response.stop_reason == "max_tokens":
            raise TruncationError(
                f"Response truncated at max_tokens ({response.usage.output_tokens} tokens). "
                "Increase max_tokens or simplify the schema."
            )
    elif hasattr(response, "choices"):
        # OpenAI
        if response.choices[0].finish_reason == "length":
            raise TruncationError("Response truncated at max_tokens.")
```

**Mitigation:** set `max_tokens` to at least 2x the expected output length. For schemas with
variable-length arrays, use generous limits. The cost of unused `max_tokens` is zero — you pay
only for tokens actually generated.

### 16.2 Infinite retry loops

Without a retry bound, semantic validation failures can produce infinite loops: the model fails
validation, retries, produces a different but still invalid response, retries again. The output
repair prompt grows with each retry (it includes previous failures), consuming more tokens and
producing longer responses that may trigger new validation failures.

**Detection:** the retry counter in §7.

**Mitigation:**
- Hard retry limit (2–3 attempts).
- Circuit breaker (§7.5).
- Fallback schema (§7.4).
- Track retry rates in observability (`10`). A rising retry rate is a leading indicator of a
  model-version or schema-incompatibility issue.

### 16.3 Schema drift

The prompt template says one thing; the Pydantic model says another; the API documentation says
a third. Schema definitions maintained in multiple places drift out of sync.

**Detection:** the snapshot tests in §14.4.

**Mitigation:** single source of truth. Define the schema in Pydantic, generate JSON Schema from
it, and use the Pydantic model for both the generation prompt (via `.model_json_schema()`) and the
validation step. Never maintain a separate schema definition.

```python
# Single source of truth: the Pydantic model
class AnswerSchema(BaseModel):
    ...

# For the generation prompt (tool definition)
tool_schema = AnswerSchema.model_json_schema()

# For API documentation (OpenAPI)
openapi_schema = AnswerSchema.model_json_schema()

# For validation (runtime)
validated = AnswerSchema.model_validate(raw_output)

# All three derive from the same class. Drift is impossible.
```

### 16.4 Model version changes breaking output format

A model update changes how the model interprets schema instructions. Fields that were reliably
populated become empty. Enum values shift. Confidence scores recalibrate. This is the structured-
output equivalent of a database migration: the producer changed and the consumers didn't.

**Detection:** the regression suite in §14.6.

**Mitigation:**
- Pin model versions in production (`claude-sonnet-4-20250514`, not `claude-sonnet-4`).
- Run the regression suite before promoting a new model version.
- Monitor validation pass rates in production; alert on >2% change.
- Maintain a model-version changelog that tracks output behavior, not just model capabilities.

### 16.5 Refusal masquerading as compliance

The model declines to answer but produces a schema-conformant response:

```json
{
  "answer": "I'm sorry, but I cannot provide information about that topic.",
  "citations": [],
  "confidence": 0.0,
  "unanswerable": false
}
```

This passes structural validation and even most semantic validation. The `unanswerable` field is
`false` (inconsistent with the refusal), but the model may not "realize" it is refusing.

**Detection:** pattern matching on the answer text, combined with the `confidence` check.

```python
REFUSAL_PATTERNS = [
    r"I('m| am) (sorry|unable|not able)",
    r"I can('t|not) (provide|answer|help with)",
    r"(As an AI|I don't have|I cannot)",
    r"(inappropriate|not appropriate|outside my)",
]

def detect_soft_refusal(response: RAGResponse) -> bool:
    """Detect refusals that passed structural validation."""
    import re
    for pattern in REFUSAL_PATTERNS:
        if re.search(pattern, response.answer, re.IGNORECASE):
            return True
    return False
```

### 16.6 Context window overflow

The assembled prompt exceeds the model's context window. This is primarily `06`'s problem, but
generation must handle it as a failure mode:

```python
import tiktoken

def check_context_budget(
    prompt_tokens: int,
    max_output_tokens: int,
    model_context_limit: int,
) -> None:
    """Verify the prompt fits within the model's context window with room for output."""
    available = model_context_limit - max_output_tokens
    if prompt_tokens > available:
        raise ContextOverflowError(
            f"Prompt ({prompt_tokens} tokens) exceeds available context "
            f"({available} tokens = {model_context_limit} limit - {max_output_tokens} reserved for output). "
            f"Reduce context by {prompt_tokens - available} tokens."
        )
```

### 16.7 Cascade failures in agent loops

In agent architectures (`13`), generation output feeds into the next step's input. A malformed
generation output causes the next step to misinterpret the action, which produces a worse context
for the following generation, which produces a worse output. The error compounds across turns.

**Detection:** per-turn validation in the agent loop.

**Mitigation:** validate *every* generation output, even intermediate ones, before passing it to
the next step. An agent loop without per-turn validation is a pipeline without per-stage
monitoring — you discover failures at the end, when the context is gone.

---

## 17. Anti-patterns

**Prompting for JSON without enforcement.** "Please respond in JSON format" in the system prompt,
followed by `json.loads()` on the raw output. Works 95% of the time; the other 5% are
production incidents. Use constrained decoding (§3, §4).

**Validating syntax but not semantics.** The response is valid JSON matching the schema, but
citations reference nonexistent chunks, confidence is 0.99 with no supporting evidence, and the
answer contradicts the context. Layer 3 validation (§6.3) exists for this.

**Unbounded retries.** Retrying until it works, without a retry limit or circuit breaker. Three
retries on a failing query cost 4x the normal budget and add 4x the latency. Use §7.5's circuit
breaker and §7.4's fallback schema.

**Verbose schemas for token-dominated workloads.** A 30-field schema with paragraph-length
descriptions on a system processing 10M queries/month. Field descriptions are prompt engineering
and should be optimized like prompts — tested for steering effectiveness, trimmed when redundant
(§13).

**Caching structured output without schema versioning.** A cached response from schema v1 is
served to a client expecting schema v2. Add `schema_version` to cached entries and invalidate
on version change (§5.5).

**Testing structured output with `assert response is not None`.** A test that passes on any
non-null response is not a test. Use property-based testing (§14.2), validator unit tests (§14.3),
and regression suites (§14.6).

**Using the most expensive model for every generation step.** An extraction step that maps
reasoning to a Pydantic model does not need Claude Opus — Haiku usually suffices. Match model
tier to task complexity, and measure the retry rate to confirm (§15.4).

**Ignoring stop reason.** Processing the output without checking whether it was truncated. A
truncated response is not a valid response, even if the partial JSON happens to parse. Always
check the stop reason (§16.1).

**Streaming structured output and validating only at the end.** The user watches tokens stream
for 20 seconds, then the response vanishes on validation failure. Use field-level validation
during streaming (§10.5) or the dual-stream pattern.

**Regex parsing of LLM output.** A regex that extracts fields from free text. Every regex is a
new parser, every parser has edge cases, and the model's output format varies across runs (§2.2).
Use the provider's structured output mechanism instead.

**Single schema for all query types.** A flat schema with 25 optional fields, most of which are
null on any given response. Use discriminated unions (§5.4) — one schema per query type, selected
by a discriminator field.

**No degradation path.** When generation fails (all retries exhausted, circuit breaker tripped),
the system returns a 500 error. The user gets nothing. Define a degradation path: a cached
response, a simpler answer, a human-readable error with the retrieved context attached (§2.3).

**Optimizing field names for token savings before caching the schema.** Saving 50 tokens per
response by renaming `verbatim_quote` to `q`, when caching the schema saves 1,350 tokens per
response. Do the cheap optimization first (§13.3).

**Evaluating generation with exact string matching.** "The capital of France is Paris." and
"Paris is the capital of France." are both correct. Use semantic similarity or LLM-judge
evaluation (`08` §10, §11), not string equality.

**Using temperature > 0 for structured output.** Temperature injects randomness into value tokens
(the answer text, confidence scores), not into structural tokens (which are constrained). The
result is noise, not creativity. Use `temp=0` for structured output and inject diversity at a
higher level if needed (§8.4).

---

## 18. Mental models — the compressed set

1. **Generation is where the data system meets the text system.** The tension between freedom and
   structure governs every design decision in this chapter. Resolve it with a contract, not a
   prayer (§1, §2).

2. **Structured output is a contract with four parts: schema, validation, retry, degradation.**
   Missing any one is a latent incident (§2.3).

3. **Constrained decoding eliminates structural failures but not semantic failures.** Layer 3
   validation — checking that citations exist, confidence is calibrated, claims are grounded — is
   never free (§4, §6.3).

4. **The schema is a prompt.** Field names, descriptions, and structure steer the model. A
   well-named field with a good description produces better output than a terse field with a
   paragraph of instructions in the system prompt (§5.1, §5.2).

5. **Output repair is the most effective retry strategy.** Re-prompting with the validation error
   produces better results than re-prompting from scratch, because the model sees what went wrong
   (§7.3).

6. **Use `temperature=0` for structured output.** The value tokens benefit from greedy decoding;
   the structural tokens are constrained anyway. Temperature adds noise to confidence scores
   and citation selection — variance you do not want (§8.4).

7. **Citations are a system design problem, not a prompt engineering problem.** Design them into
   the schema, verify them programmatically, and measure citation density as a quality signal
   (§9).

8. **Every retry doubles the cost of a failing query.** Bound retries, implement circuit breakers,
   and define a fallback schema. The cost of retries is the strongest argument for constrained
   decoding: zero structural retries (§7.6).

9. **Context tokens dominate input cost; output tokens are 3–5x more expensive per token.**
   Optimize context first (`06`), then structural output overhead. Cache the schema prefix — it
   is the cheapest optimization available (§15.3, §13.5).

10. **Pin model versions in production.** A model update is a schema migration. Run regression
    tests before promoting (§16.4).

11. **Use the cheapest model that passes validation at < 5% retry rate.** Measure, do not assume.
    Haiku-class models handle extraction and classification; Sonnet-class models handle reasoning
    and synthesis (§15.4).

12. **The degradation path is the most important part of the contract.** When generation fails,
    what does the user see? Define it before the first incident, not during it (§2.3, §7.4).

13. **Multi-step generation trades latency for quality.** Think-then-extract, self-critique loops,
    and extended thinking all add round-trips. Measure the quality improvement on your eval set
    before enabling in production. The marginal gain from the second refinement is almost always
    smaller than the first (§11).

14. **Streaming and validation are in tension.** The user sees tokens arriving; validation
    requires the full response. Resolve with field-level early validation, the dual-stream
    pattern, or optimistic display with post-hoc correction (§10.5).

15. **Test the contract, not the text.** Property-based tests on the schema, unit tests on the
    validators, regression suites across model versions, and snapshot tests on the prompt. String
    equality is not a test for generation output (§14).

---

## 19. Lab exercises

**Lab 1 — Build the structured output contract for a RAG answer.**
*Goal:* define the schema, validation layer, retry strategy, and degradation path for a
single-question RAG system.
*Steps:* (a) Define a Pydantic model for the answer schema, including claims, citations with
verbatim quotes, and confidence. (b) Implement the three validation layers: syntactic, structural,
semantic. The semantic layer must verify that citation `chunk_id` values exist in the retrieved
set and that `verbatim_quote` values appear in the source chunks. (c) Implement the retry loop
with output repair (§7.3): on semantic validation failure, re-prompt with the error. (d) Implement
a fallback schema (§7.4) for when retries are exhausted. (e) Wire it all together into a single
`generate_validated_answer()` function.
*Artifact:* a working `generation.py` module with tests. The tests must include at least one case
where the mock model returns a hallucinated citation, triggering a retry.
*Success criterion:* the function handles all four branches of the retry decision tree (§7.2) and
the test suite covers each branch.
*Time:* ~4 hours.
*Unblocks:* Lab 2, Lab 4, and every downstream exercise that needs a structured answer.

**Lab 2 — Verbatim citation verification on your own corpus.**
*Goal:* measure how often the model produces genuinely verbatim quotes vs. paraphrases vs.
fabricated citations.
*Steps:* run 50 queries from your golden set (`08` Lab 1) through the generation module from
Lab 1. For each citation in each response, run the verbatim verification function from §9.4.
Classify each citation as exact-match, normalized-match (whitespace differences only),
paraphrase (fuzzy match > 0.85), or fabricated (fuzzy match < 0.85). Report the distribution.
*Artifact:* a table: `{query_id, citation_index, match_type, similarity_score}`. Plus aggregate
numbers: exact-match rate, paraphrase rate, fabrication rate.
*Success criterion:* you can state your model's verbatim citation accuracy as a number with
a category breakdown. If the fabrication rate is > 5%, identify the query types where it
concentrates.
*Time:* ~3 hours.
*Unblocks:* `08` §10's faithfulness evaluation with citation-level granularity.

**Lab 3 — Measure the retry budget.**
*Goal:* quantify the cost of retries in your generation pipeline.
*Steps:* run 200 queries through the generation module from Lab 1. Log: total attempts per query,
tokens consumed per attempt, validation errors per attempt, final status (accepted / degraded /
failed). Compute: mean and p95 retries per query, total token cost with vs. without retries,
percentage of queries that needed retries, percentage that exhausted all retries.
*Artifact:* a cost table matching §15.3's format but with your real numbers. Plus a histogram
of retries-per-query.
*Success criterion:* you can state the expected cost multiplier from retries (e.g., "retries add
8% to total generation cost") and the fraction of queries that degrade.
*Time:* ~2 hours.
*Unblocks:* the cost model in §15 with real numbers; circuit-breaker threshold tuning.

**Lab 4 — Schema complexity vs. output quality tradeoff.**
*Goal:* find the schema complexity sweet spot for your use case.
*Steps:* define three schemas of increasing complexity: (a) simple — answer + confidence; (b)
medium — answer + claims + citations (chunk_id only) + confidence; (c) full — answer + claims
with verbatim quotes + inline citation markers + confidence + unanswerable_reason. Run 50
queries through each schema. Measure: validation pass rate, faithfulness (via `08` Lab 4's
judge), token count, latency.
*Artifact:* a table with one row per schema and columns for pass rate, faithfulness, mean output
tokens, mean latency, mean cost per query.
*Success criterion:* you can state which schema gives the best quality-per-dollar, and whether
the full schema's additional cost is justified by its additional quality.
*Time:* ~4 hours.
*Unblocks:* schema selection for production; token-efficiency optimization.

**Lab 5 — Model tiering for multi-step generation.**
*Goal:* find the cheapest model that works for each step in a think-then-extract pipeline.
*Steps:* implement the think-then-extract pattern (§11.2) with configurable models for each step.
Run 50 queries with four configurations: (a) Sonnet/Sonnet, (b) Sonnet/Haiku, (c) Haiku/Haiku,
(d) Opus/Haiku. Measure: validation pass rate, faithfulness, total cost, total latency.
*Artifact:* a four-row comparison table with cost and quality metrics.
*Success criterion:* you can name the cheapest configuration that maintains > 95% validation pass
rate and faithfulness within 0.05 of the best configuration.
*Time:* ~4 hours.
*Unblocks:* production model selection; cost optimization.

**Lab 6 — Streaming structured output with early validation.**
*Goal:* build a streaming generation endpoint that validates fields as they complete.
*Steps:* (a) implement an SSE endpoint that streams generation output. (b) implement field-level
validation: as each field completes in the partial JSON stream, validate it immediately. (c) if
early validation detects a likely failure (e.g., empty citations array when `unanswerable` is
false), log the warning immediately, before the response completes. (d) measure: TTFT, total
latency, and time-to-first-validated-field.
*Artifact:* a working streaming endpoint with early-validation logging. Include a screenshot
or recording of the streaming UI.
*Success criterion:* the endpoint detects at least one cross-field violation *before* the
response completes, in the test suite.
*Time:* ~5 hours.
*Unblocks:* `12`'s streaming architecture with validation-aware generation.

**Lab 7 — Prompt caching impact on generation cost.**
*Goal:* measure the actual (not estimated) cost reduction from caching the schema prefix.
*Steps:* run 100 queries in two configurations: (a) without caching; (b) with the system prompt
and tool definitions marked as cacheable. For each request, log `usage.cache_creation_input_tokens`
and `usage.cache_read_input_tokens` from the API response. Compute: actual cache hit rate,
effective input token cost per query, total cost savings.
*Artifact:* a two-row cost table (cached vs. uncached) with real token counts from the API, not
estimates. Plus the cache-hit-rate trajectory over the 100 requests (the first request is always
a miss).
*Success criterion:* you can state the actual cache hit rate and the actual cost reduction, and
explain any cache misses you observe (e.g., cold start, cache eviction).
*Time:* ~2 hours.
*Unblocks:* `12`'s caching section with real numbers.

**Lab 8 — Property-based testing for your generation schema.**
*Goal:* find edge cases in your validation layer that manual tests missed.
*Steps:* (a) install `hypothesis` and `hypothesis-jsonschema`. (b) write property-based tests
for your schema (§14.2): roundtrip serialization, validator-no-crash, and at least one domain
invariant (e.g., "if unanswerable is true, confidence must be < 0.3"). (c) run with 1000 examples.
(d) fix any failures — these are real bugs in your validators.
*Artifact:* the property-based test file, plus a log of the bugs found and fixed.
*Success criterion:* all property-based tests pass at 1000 examples, and you found at least one
validator bug that the manual test suite in Lab 1 missed.
*Time:* ~3 hours.
*Unblocks:* confidence in the validation layer for production deployment.

**Lab 9 — Model version regression test.**
*Goal:* build a regression gate that catches structured output breakage from model version changes.
*Steps:* (a) run your 50-query eval set against two model versions (e.g., Sonnet dated variants,
or Sonnet vs. Haiku). (b) compare: validation pass rate, field-level agreement (does the same
query produce the same `unanswerable` classification?), citation count distribution, confidence
calibration. (c) implement an automated gate: the new model version is accepted if pass rate
drops by < 2% and field-level agreement is > 90%.
*Artifact:* a comparison report plus a `check_model_regression()` function that returns pass/fail.
*Success criterion:* you can state whether the new model version is safe to promote, with numbers
backing the decision.
*Time:* ~4 hours.
*Unblocks:* safe model version upgrades in production; `08` §14's gate infrastructure.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — why constrained decoding
eliminates structural retries, why citation verification catches hallucination, why temperature=0
is correct for structured output, why the retry cost multiplier bounds at `(1 + retry_rate ×
retries_per_failure)` — are derivable from the definitions and verifiable from the code in this
chapter. The cost arithmetic in §15.3, the overhead analysis in §13.2, and every formula in §1.4
are derivations, not measurements: every input is labeled as an assumption and every output is
checkable with an interpreter.

The pricing figures in §15.2 are current-as-of Anthropic's and OpenAI's published pricing pages
cached mid-2025. Pricing changes; re-check before quoting a dollar figure. The *shape* of the
argument (output tokens cost 3–5x more, caching reduces input cost ~10x, batching reduces cost
~50%, model tiering is a 10–20x lever) is stable; the digits are not.

Deliberately **not** in this document: any absolute quality number for any schema design, any
claim about which model produces the best structured output, and any threshold presented as
universal. Every threshold here (< 5% retry rate, > 90% field agreement, > 0.85 fuzzy match for
citation verification) is a *starting point argued from a stated rationale*, and the chapter's own
thesis is that these must be re-derived on your data. The first rung-1 numbers for this chapter
come from the labs in §19, which produce your own citation accuracy, your own retry budget, your
own cost model, and your own regression gate — each carrying its own one-sentence account of how
it was measured, per README §6's rule. This document itself stays rung 3; it is the map, not
the territory.
