# 05 — Query understanding: rewriting, decomposition, and HyDE

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow and the
> recall ceiling — query understanding exists to push the ceiling upward from the query side, where
> `04` pushed it from the retrieval side), [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md)
> (§3 on asymmetric embedding — `input_type` is one of the mechanisms HyDE exploits, and §4's
> geometry of the embedding space is what every rewrite strategy is implicitly navigating),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§4.3 — the
> canonical chunk text is the target your manufactured queries need to land near, and §5's size
> decisions determine how much vocabulary overlap a query needs to hit),
> [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md) (§3's operating point
> determines how many extra queries you can afford, and §7's filtered search means some
> rewrites need to carry metadata predicates through),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (the cascade — query
> understanding manufactures additional *branches* into §5's fusion step, and the cascade property
> means these branches can only raise the recall ceiling, never lower it, provided fusion handles
> them correctly).
>
> **Feeds into:** [`06-context-engineering.md`](06-context-engineering.md) (more retrieval inputs mean
> more candidates, which means a harder context-budgeting problem — §12's pipeline is the input to
> `06`'s token budget), [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§13's ablation
> methodology is the only honest way to answer "did the rewrite help?"),
> [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md) (every LLM call in
> query understanding is a span that must be traced — if you cannot see the rewritten query in your
> traces, you cannot debug retrieval failures),
> [`12-serving-latency-and-caching.md`](12-serving-latency-and-caching.md) (§10's cost model is where
> your latency budget comes from, and §11's caching strategy is the main lever for amortizing the
> added LLM calls),
> [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md) (agentic RAG runs query
> understanding in a loop — everything here multiplies),
> [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md) (a
> rewritten query is an LLM output used as a retrieval input — it is an injection surface, and §14
> here is where that risk materializes).
>
> **THESIS:** a query is **not** the retrieval input. It is the raw material from which one or more
> retrieval inputs are *manufactured*. The gap between what a user types and what the index needs is
> the single largest source of retrieval failure after the recall ceiling (`04` §1), and closing it
> is query understanding's job. Every technique here — rewriting, decomposition, HyDE,
> multi-query — is a different strategy for manufacturing better retrieval inputs from the same raw
> query. Each one adds an LLM call, which means latency and cost. The design question is never
> "should we rewrite queries?" It is: *which manufacturing strategies close which kinds of gaps, at
> what cost, and under what conditions does the gap they close exceed the cost they add?*
>
> The answer is empirical, per-corpus, and measurable in an afternoon — and the teams that skip the
> measurement are the ones running four LLM calls per query to rewrite a question that would have
> retrieved perfectly well as-is.

---

## Contents

1. [The gap between what users type and what indexes need](#1-the-gap-between-what-users-type-and-what-indexes-need)
2. [Query classification and routing](#2-query-classification-and-routing)
3. [Query rewriting with LLMs](#3-query-rewriting-with-llms)
4. [Step-back prompting](#4-step-back-prompting)
5. [Query decomposition](#5-query-decomposition)
6. [Multi-query generation](#6-multi-query-generation)
7. [HyDE — Hypothetical Document Embeddings](#7-hyde--hypothetical-document-embeddings)
8. [Query expansion — classical and modern](#8-query-expansion--classical-and-modern)
9. [Conversational query resolution](#9-conversational-query-resolution)
10. [The cost model for query understanding](#10-the-cost-model-for-query-understanding)
11. [Caching query transformations](#11-caching-query-transformations)
12. [Combining techniques: the query preprocessing pipeline](#12-combining-techniques-the-query-preprocessing-pipeline)
13. [Evaluation of query understanding](#13-evaluation-of-query-understanding)
14. [Failure modes and debugging](#14-failure-modes-and-debugging)
15. [Anti-patterns](#15-anti-patterns)
16. [Mental models — the compressed set](#16-mental-models--the-compressed-set)
17. [Lab exercises](#17-lab-exercises)

---

## 1. The gap between what users type and what indexes need

A user types "why is my deployment failing." The index contains a chunk whose first sentence is
"Container images that exceed the 10 GiB layer-size limit are rejected during the push phase of the
CI/CD pipeline." There is no vocabulary overlap beyond the stop words. The dense embedding model
*might* bridge that gap, but "failing" is vague enough and "deployment" is broad enough that the
embedding will be near dozens of chunks about deployments, very few of which explain this specific
failure. The lexical branch will return nothing useful. The retrieval system will fail, and from the
outside it will look like a retrieval quality problem when it is in fact a *query quality* problem.

This is not an edge case. It is the dominant case. Real user queries are:

- **Under-specified.** "how does auth work" — which auth? For which service? The user knows what
  they mean; the index does not.
- **Vocabulary-mismatched.** The user says "error" and the corpus says "exception." The user says
  "slow" and the corpus says "latency." The user says "broken" and the corpus describes a specific
  failure mode.
- **Multi-part without structure.** "Can I use feature X with feature Y, and if so, does it affect
  billing?" — this is three retrieval needs in one sentence, and no single chunk answers all of
  them.
- **Conversational and anaphoric.** "What about the pricing?" — pricing of what? The referent is
  three turns back.
- **At the wrong level of abstraction.** The user asks a specific question whose answer requires
  understanding a general concept that the corpus explains in its own terms.

### 1.1 A taxonomy of query-index mismatches

The gap is not one gap. It is a family of distinct mismatches, each requiring a different
manufacturing strategy:

| Mismatch type | Example | Why raw retrieval fails | Manufacturing strategy |
|---|---|---|---|
| Vocabulary | "cancel" vs. "terminate subscription" | no lexical overlap; dense embedding may not bridge domain-specific synonyms | Query rewriting (§3) |
| Abstraction level | "why is my pod evicted" vs. chunk about Kubernetes resource limits | query is at symptom level, corpus is at mechanism level | Step-back prompting (§4) |
| Complexity | "compare X and Y for use case Z" | no single chunk answers this; answer requires synthesizing multiple chunks | Decomposition (§5) |
| Perspective | user describes the problem; corpus describes the solution | embedding of the problem and embedding of the solution are not nearest neighbors | HyDE (§7) |
| Ambiguity | "how does it handle errors" — "it" is undefined | the query vector is a blend of many possible meanings | Conversational resolution (§9) |
| Incompleteness | "auth" when the user means "OAuth 2.0 PKCE flow for mobile" | the query has too little signal to discriminate | Query expansion (§8) |
| Granularity | user wants a one-line answer; corpus has only long explanations, or vice versa | chunk granularity from `02` does not match query granularity | Multi-query at different granularities (§6) |

The first step of query understanding is *classifying* which of these mismatches applies, because
applying the wrong manufacturing strategy wastes an LLM call and can make retrieval worse (§14).

### 1.2 The manufacturing metaphor, precisely

```
    raw query (what the user typed)
        │
        ├── classify ──────────────────────────────────────────────┐
        │                                                          │
        │   ┌─────────────────────────────────────────────┐       │
        │   │  query understanding layer                  │       │
        │   │                                             │       │
        │   │  ┌───────────┐  ┌──────────┐  ┌─────────┐  │       │
        │   │  │ rewrite   │  │decompose │  │  HyDE   │  │       │
        │   │  └─────┬─────┘  └────┬─────┘  └────┬────┘  │       │
        │   │        │             │              │       │       │
        │   │        ▼             ▼              ▼       │       │
        │   │  ┌─────────────────────────────────────┐    │       │
        │   │  │ manufactured retrieval inputs        │    │  routing
        │   │  │ (1 to N queries + 0 to M embeddings) │    │  decision
        │   │  └────────────────┬────────────────────┘    │       │
        │   └───────────────────┼──────────────────────────┘       │
        │                       │                                  │
        ▼                       ▼                                  ▼
    ┌────────────────────────────────────────────────────────────────┐
    │  retrieval cascade (04)                                       │
    │  each manufactured input is a branch into fusion              │
    └────────────────────────────────────────────────────────────────┘
```

The key structural observation: **each manufactured retrieval input becomes an additional branch in
`04`'s cascade.** The cascade property still holds — each branch can only add candidates, never
remove them — so the worst case of a bad rewrite is that it adds irrelevant candidates that the
reranker discards. But the best case is that it adds candidates the original query would never have
found.

This is why query understanding operates *before* retrieval, not instead of it. The original query
still runs. The manufactured queries run alongside it. Fusion and reranking handle the rest.

### 1.3 The recall ceiling revisited

`04` §1 established that first-stage recall is the ceiling for everything downstream. Query
understanding is the *other* lever for raising that ceiling, complementary to adding retrieval
branches:

```
    recall_ceiling = recall(union of all retrieval branches at their candidate depths)
```

Adding a BM25 branch to a dense-only system raises the ceiling by covering lexical-match queries.
Rewriting a vague query into a precise one raises the ceiling by making the existing dense branch
*actually find* the relevant chunks. Decomposing a complex query into sub-queries raises the ceiling
by retrieving chunks that no single query variant would have found.

All three are the same operation at the architectural level — adding more candidates to the union
before fusion — but they differ in *what they cost*. Adding BM25 costs a sub-millisecond index
lookup. Rewriting costs an LLM call. That cost difference is why §10 exists.

---

## 2. Query classification and routing

Not every query needs manufacturing. A precise, well-formed query with domain-specific vocabulary
that matches the corpus will retrieve well as-is. Running it through a rewriter adds latency,
cost, and the risk that the rewrite loses precision (§14.1). The first job of query understanding
is to decide which queries need which manufacturing strategies — and which need none.

### 2.1 Intent classification

Intent is a coarser signal than it looks. For query understanding routing, the relevant intents are
not the application's semantic intents (e.g., "the user wants to cancel their subscription") but
the *retrieval intents* — what kind of retrieval behavior the query needs:

| Retrieval intent | Description | Routing implication |
|---|---|---|
| Lookup | user wants a specific fact | likely well-formed; pass through or light rewrite |
| Explanation | user wants to understand a concept | may need step-back to retrieve conceptual chunks |
| Comparison | user wants to compare two or more items | decompose into per-item sub-queries |
| Troubleshooting | user describes a symptom, wants a cause | HyDE is strong here — generate the diagnosis, embed that |
| Procedural | user wants step-by-step instructions | likely well-formed if vocabulary matches; rewrite if not |
| Aggregation | user wants a summary across many items | decompose, then the answer is a synthesis problem for `06` |
| Conversational follow-up | user references prior context | resolve coreferences first (§9), then classify again |

### 2.2 Complexity estimation

Complexity determines whether decomposition is worth the cost. A single-hop question ("What is the
default timeout for service X?") needs at most a rewrite. A multi-hop question ("How does the retry
policy interact with the circuit breaker when the upstream is rate-limited?") needs decomposition
into sub-queries that each retrieve different chunks.

A simple heuristic that works surprisingly well:

```python
from dataclasses import dataclass
from enum import Enum
import re


class QueryComplexity(Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    CONVERSATIONAL = "conv"


@dataclass
class ClassificationResult:
    complexity: QueryComplexity
    intent: str
    needs_resolution: bool
    confidence: float
    reasoning: str


def classify_query_heuristic(query: str, has_chat_history: bool = False) -> ClassificationResult:
    """Rule-based pre-classifier.  Cheap enough to run on every query.
    Falls back to LLM classification only when confidence < 0.6.
    """
    query_lower = query.lower().strip()
    tokens = query_lower.split()
    
    # Conversational signals
    has_pronouns = bool(re.search(r'\b(it|this|that|these|those|its|they|them)\b', query_lower))
    
    if has_pronouns and has_chat_history:
        return ClassificationResult(QueryComplexity.CONVERSATIONAL, "follow_up", True, 0.8,
                                    "Pronouns detected with active chat history")
    if len(tokens) <= 4 and has_chat_history:
        return ClassificationResult(QueryComplexity.CONVERSATIONAL, "follow_up", True, 0.7,
                                    "Very short query with active chat history")
    
    # Complexity scoring
    comparison_words = {"compare", "versus", "vs", "differ", "difference", "between"}
    has_comparison = bool(comparison_words & set(tokens))
    conjunction_count = sum(1 for t in tokens if t in {"and", "also", "additionally", "both"})
    
    complexity_score = (
        (1.5 if has_comparison else 0)
        + conjunction_count * 0.5
        + max(0, (query.count("?") - 1)) * 1.0
        + (0.5 if {"if", "when", "unless"} & set(tokens) else 0)
        + query.count(",") * 0.3
    )
    
    if complexity_score >= 2.0:
        complexity = QueryComplexity.COMPLEX
    elif complexity_score >= 0.8 or len(tokens) > 15:
        complexity = QueryComplexity.MODERATE
    else:
        complexity = QueryComplexity.SIMPLE
    
    # Intent (coarse)
    if has_comparison:
        intent = "comparison"
    elif any(w in tokens for w in ["how", "why", "explain"]):
        intent = "explanation"
    elif any(w in tokens for w in ["error", "fail", "broken", "issue", "bug", "wrong"]):
        intent = "troubleshooting"
    elif any(w in tokens for w in ["steps", "guide", "tutorial"]):
        intent = "procedural"
    else:
        intent = "lookup"
    
    return ClassificationResult(complexity, intent, False, 0.6,
                                f"Heuristic: score={complexity_score:.1f}, tokens={len(tokens)}")
```

This heuristic is not accurate enough for high-stakes routing. Its job is to be *cheap enough to
run on every query* and to provide a prior that the LLM classifier can override. The LLM classifier
is the expensive, accurate path — and the routing decision is about whether to spend that cost.

### 2.3 LLM-based classification

When the heuristic is uncertain (confidence below threshold) or when you need the LLM classifier's
structured output for downstream routing:

```python
import json
from typing import Any

CLASSIFICATION_PROMPT = """\
You are a query classifier for a retrieval system. Return a JSON object with:
1. "complexity": "simple" | "moderate" | "complex"
2. "intent": "lookup" | "explanation" | "comparison" | "troubleshooting" | "procedural" | "aggregation"
3. "strategy": list from ["passthrough", "rewrite", "decompose", "hyde", "expand", "step_back"]
4. "reasoning": one sentence

Rules:
- "simple" queries: at most "passthrough" or "rewrite"
- "complex" queries: typically "decompose", possibly others for sub-queries
- "troubleshooting": benefits from "hyde" (generate likely diagnosis, search for it)
- Return ONLY the JSON object.

Query: {query}
"""


async def classify_query_llm(
    query: str, llm_client: Any, model: str = "claude-sonnet-4-20250514",
) -> dict:
    """LLM-based classification.  Uses a fast model — this is routing, not generation."""
    response = await llm_client.messages.create(
        model=model, max_tokens=256, temperature=0.0,
        messages=[{"role": "user", "content": CLASSIFICATION_PROMPT.format(query=query)}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)
```

### 2.4 The routing table

Once classification produces a strategy list, the router dispatches to the appropriate
manufacturing pipeline. This is a table, not a decision tree, because the strategies are
combinable:

```python
@dataclass
class RoutingDecision:
    original_query_runs: bool = True
    rewrite: bool = False
    decompose: bool = False
    hyde: bool = False
    expand: bool = False
    step_back: bool = False
    resolve_coreferences: bool = False
    max_manufactured_queries: int = 5


def route(
    classification: ClassificationResult | dict,
    latency_budget_ms: float = 2000.0,
) -> RoutingDecision:
    """Map classification to routing decision, gated by latency budget."""
    if isinstance(classification, dict):
        strategies = classification.get("strategy", ["passthrough"])
        complexity = classification.get("complexity", "simple")
    else:
        strategies = _heuristic_to_strategies(classification)
        complexity = classification.complexity.value
    
    decision = RoutingDecision()
    if isinstance(classification, ClassificationResult) and classification.needs_resolution:
        decision.resolve_coreferences = True
    if "passthrough" in strategies:
        return decision
    
    remaining_ms = latency_budget_ms - 200   # reserve for retrieval+rerank
    llm_call_ms = 500                         # conservative per-call estimate
    
    for strategy in strategies:
        if remaining_ms < llm_call_ms:
            break
        if strategy == "rewrite":
            decision.rewrite = True
        elif strategy == "decompose":
            decision.decompose = True
            decision.max_manufactured_queries = min(decision.max_manufactured_queries,
                                                    4 if complexity == "complex" else 2)
        elif strategy == "hyde":
            decision.hyde = True
        elif strategy == "expand":
            decision.expand = True
        elif strategy == "step_back":
            decision.step_back = True
        else:
            continue
        remaining_ms -= llm_call_ms
    
    return decision
```

### 2.5 Why routing matters more than any single technique

The literature and the tutorial ecosystem are full of papers and blog posts advocating for
individual techniques — "always rewrite," "always use HyDE," "always decompose." In practice,
the dominant effect of query understanding is not which technique you use but *whether you apply
the right technique to the right query*. A well-routed system that uses simple rewriting on
vocabulary-mismatch queries and decomposition on complex queries will outperform a system that
runs every query through a fixed pipeline of all techniques — and it will do so at a fraction of
the cost (§10).

The routing decision is the query understanding layer's most important output. Everything else is
implementation of that decision.

---

## 3. Query rewriting with LLMs

Rewriting is the simplest and most broadly applicable manufacturing strategy: take the raw query,
pass it through an LLM with instructions to produce a better retrieval query, use the rewritten
query for retrieval.

### 3.1 What rewriting does and does not do

Rewriting addresses a specific class of query-index mismatch: **vocabulary mismatch**, where the
user's words are semantically correct but lexically distant from the corpus's words. It can also
fix minor grammatical issues, expand abbreviations, and clarify ambiguous phrasings.

What it cannot do:

- **Decompose** — a rewrite is still one query, so it cannot handle multi-part questions
- **Bridge abstraction levels** — "why is my pod failing" rewritten as "why is my Kubernetes pod
  failing" is still at the symptom level; step-back prompting (§4) handles this
- **Generate the answer's perspective** — HyDE (§7) handles this

### 3.2 Prompt templates for query rewriting

The prompt template is the entire mechanism. Its quality determines whether rewriting helps or
hurts. Here is a template that works well in practice, with annotations on why each part matters:

```python
REWRITE_PROMPT = """\
You are a search query optimizer for a technical documentation system.

Your job: rewrite the user's question into a search query that will retrieve the most relevant
documentation chunks.

Rules:
1. Preserve the user's INTENT exactly — do not answer the question, do not add information
   the user did not ask for, do not change what they are looking for.
2. Replace vague terms with specific technical terms where the intent is clear.
   Example: "slow" -> "high latency" or "performance degradation"
3. Expand abbreviations that the documentation would spell out.
   Example: "k8s" -> "Kubernetes", "OOM" -> "out of memory"
4. Remove conversational filler that adds no retrieval signal.
   Example: "Hey, can you help me understand" -> remove
5. Keep proper nouns, version numbers, error codes, and identifiers EXACTLY as written.
6. Output ONLY the rewritten query, nothing else.
7. If the query is already well-formed for retrieval, return it unchanged.

User query: {query}
Rewritten query:"""


async def rewrite_query(
    query: str,
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
    domain_context: str = "",
) -> str:
    """Rewrite a query for better retrieval.
    
    Returns the original query if the LLM determines no rewrite is needed,
    or if the rewrite call fails (fail-open: original query always runs).
    """
    prompt = REWRITE_PROMPT.format(query=query)
    if domain_context:
        prompt = f"Domain context: {domain_context}\n\n{prompt}"
    
    try:
        response = await llm_client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        rewritten = response.content[0].text.strip()
        
        # Safety checks
        if len(rewritten) > len(query) * 3:
            # Rewrite is suspiciously long — likely hallucinated content
            return query
        if len(rewritten) < 3:
            return query
        
        return rewritten
    except Exception:
        # Fail open: use the original query
        return query
```

### 3.3 Few-shot rewriting

Zero-shot rewriting works for generic domains but underperforms when the corpus has domain-specific
vocabulary conventions. Few-shot examples encode those conventions:

```python
FEW_SHOT_REWRITE_PROMPT = """\
Rewrite the user's search query to better match our technical documentation.
Return ONLY the rewritten query.

Examples:
User: "how to make the API faster"
Rewritten: "API performance optimization latency reduction"

User: "my container keeps dying"
Rewritten: "container OOMKilled restart CrashLoopBackOff troubleshooting"

User: "set up auth for the dashboard"
Rewritten: "configure authentication authorization dashboard SSO OIDC"

User: "k8s ingress not working with TLS"
Rewritten: "Kubernetes ingress controller TLS certificate configuration troubleshooting"

User: "{query}"
Rewritten:"""
```

The few-shot examples are doing three things: (1) showing the model the vocabulary conventions
of the corpus, (2) demonstrating the appropriate level of expansion, and (3) establishing that
the output should be a query, not a sentence. These examples should come from your actual corpus
and be validated against your actual retrieval system — `08`'s eval methodology applied to the
rewrite step specifically.

### 3.4 When rewriting helps versus hurts

Rewriting helps when:

- The user's vocabulary differs from the corpus's vocabulary
- The query contains conversational filler that dilutes the embedding
- Abbreviations need expansion
- The query is grammatically malformed (common in search-box UIs)

Rewriting hurts when:

- The query is already precise and well-formed — the rewrite can only lose information
- The query contains exact identifiers (error codes, function names) that the rewriter
  normalizes away
- The rewriter hallucinates domain-specific terms that do not appear in the corpus
- The query is ambiguous and the rewriter resolves the ambiguity *incorrectly* — now retrieval
  confidently finds the wrong thing

The critical design decision: **the original query always runs alongside the rewritten query.**
Rewriting is additive — it produces an additional branch for fusion (`04` §5), not a replacement.
This makes rewriting fail-safe: a bad rewrite adds irrelevant candidates that the reranker
discards, rather than replacing good candidates with bad ones.

```python
async def rewrite_and_retrieve(
    query: str,
    retriever: Any,
    llm_client: Any,
    top_k: int = 20,
) -> list[dict]:
    """Run original and rewritten queries, fuse results.
    
    The original query ALWAYS runs.  The rewrite is additive.
    """
    import asyncio
    
    rewritten = await rewrite_query(query, llm_client)
    
    # Run both retrievals in parallel
    original_results, rewritten_results = await asyncio.gather(
        retriever.search(query, top_k=top_k),
        retriever.search(rewritten, top_k=top_k) if rewritten != query else asyncio.sleep(0),
    )
    
    if rewritten == query or rewritten_results is None:
        return original_results
    
    # Fuse with RRF (04 §5)
    return reciprocal_rank_fusion(
        [original_results, rewritten_results],
        k=60,
    )
```

### 3.5 Rewriting for different retrieval branches

A subtlety that the tutorials miss: the optimal rewrite is different for the dense branch and the
lexical branch. The dense branch benefits from natural-language reformulation that moves the query
embedding closer to the relevant chunk embedding. The lexical branch benefits from keyword
extraction and synonym expansion that increases term overlap.

In a hybrid system, you can produce two rewrites — one for each branch:

```python
DENSE_REWRITE_PROMPT = """\
Rewrite this search query as a natural-language question that would appear in documentation.
Preserve the exact meaning. Output only the rewritten query.

Query: {query}
Rewritten:"""

LEXICAL_REWRITE_PROMPT = """\
Extract the key search terms from this query. Add synonyms and related technical terms
that documentation might use. Output as a space-separated list of terms.

Query: {query}
Terms:"""
```

This is worth the additional LLM call only when the two branches have complementary failure
modes on your query distribution — which is exactly the complementarity test from `04` §3.3,
applied to query variants rather than retrieval branches.

---

## 4. Step-back prompting

Step-back prompting addresses a specific mismatch that rewriting cannot fix: the query is at one
level of abstraction and the corpus is at another. The user asks "why is my Lambda function timing
out when calling DynamoDB?" The corpus has a chunk explaining "DynamoDB adaptive capacity and burst
capacity management" — the general concept that explains the specific symptom. A rewrite stays at
the symptom level. A step-back moves to the concept level.

### 4.1 The mechanism

Step-back prompting asks the LLM to generate a more abstract, higher-level version of the query.
Instead of searching for the specific symptom, you search for the general concept, principle, or
mechanism that would explain the symptom.

```python
STEP_BACK_PROMPT = """\
Given the following question, generate a more general, higher-level question that would help
retrieve background knowledge needed to answer the original question.

The step-back question should ask about the underlying concept, principle, or mechanism
rather than the specific case.

Examples:
Original: "Why does my Python process use 4GB of RAM when processing a 100MB CSV?"
Step-back: "How does Python manage memory allocation for data processing operations?"

Original: "Why is my Elasticsearch query returning 0 results after reindexing?"
Step-back: "How does Elasticsearch handle index aliases and mappings during reindexing?"

Original: "Why does my React component re-render when I use useContext?"
Step-back: "How does React's Context API propagation and re-rendering mechanism work?"

Original: "{query}"
Step-back:"""


async def generate_step_back_query(
    query: str,
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Generate a step-back (more abstract) version of the query."""
    response = await llm_client.messages.create(
        model=model,
        max_tokens=150,
        temperature=0.0,
        messages=[{"role": "user", "content": STEP_BACK_PROMPT.format(query=query)}],
    )
    return response.content[0].text.strip()
```

### 4.2 When step-back helps

Step-back helps for a specific query pattern: **the user describes a symptom, and the corpus
explains the mechanism.** This is common in:

- **Troubleshooting queries** — user describes what went wrong; corpus describes how things work
- **"Why" questions** — user asks about a specific case; answer requires understanding the general
  principle
- **Configuration questions** — user asks about a specific setting; corpus explains the design
  philosophy behind the setting

Step-back does *not* help when:

- The query and the corpus are at the same abstraction level
- The query is already about a general concept
- The query is a factual lookup ("what is the default value of X")
- The step-back loses too much specificity — "how does networking work" is useless

### 4.3 Step-back as a branch, not a replacement

Like rewriting, the step-back query is an additional branch. The original query retrieves chunks
at the symptom level; the step-back query retrieves chunks at the mechanism level. Both go into
fusion. This is how you get a context window that contains both "here is how burst capacity works"
(from the step-back) and "here is how to configure burst capacity for Lambda-DynamoDB interactions"
(from the original), giving the generation model enough to answer both "why" and "how to fix it."

```
    "Why is my Lambda timing out calling DynamoDB?"
        │
        ├── original query ──► retrieval ──► [Lambda timeout docs, DynamoDB timeout docs]
        │
        └── step-back query: "How does DynamoDB adaptive capacity and throttling work?"
                                    │
                                    └──► retrieval ──► [capacity management docs, throttling docs]
                                                                │
                                                    ┌───────────▼───────────┐
                                                    │  fusion (04 §5)       │
                                                    │  both sets of chunks  │
                                                    └───────────────────────┘
```

### 4.4 The abstraction-level ladder

Step-back can operate at multiple levels. One step back goes from symptom to mechanism. Two steps
back goes from mechanism to fundamental concept. How far to step back depends on the corpus —
if the corpus only explains mechanisms (not fundamentals), stepping back to fundamentals retrieves
nothing useful.

In practice, one step back is almost always sufficient. The rare cases where two steps help are
the ones where the user's question is so specific that even the mechanism-level chunks do not
address it, and the answer requires chaining: fundamental concept enables understanding of
mechanism enables answer to specific question. This is an agentic multi-hop retrieval problem
(`13`) rather than a query understanding problem.

---

## 5. Query decomposition

Decomposition handles the class of queries that are inherently multi-part: they require information
from multiple chunks that no single query variant would retrieve together. "Compare the pricing
models of service A and service B for our use case" decomposes into at least three retrieval needs:
pricing of A, pricing of B, and what "our use case" implies about which pricing dimensions matter.

### 5.1 When decomposition is necessary

The signal that a query needs decomposition is not complexity per se — a very complex single-topic
question may not need decomposition. The signal is **multiple retrieval needs**: the answer requires
information from chunks that are topically distinct enough that no single query would retrieve all
of them.

```
    ┌────────────────────────────────────────────────────────────────┐
    │  "How does service A handle auth, and can it integrate with   │
    │   our existing LDAP setup?"                                   │
    └──────────┬─────────────────────────────────────────────────────┘
               │
               │  decomposition
               │
    ┌──────────▼─────────────────────────────────────────────────────┐
    │  Sub-query 1: "service A authentication mechanism"            │
    │  Sub-query 2: "service A LDAP integration support"            │
    │  Sub-query 3: "service A directory service compatibility"     │
    └────────────────────────────────────────────────────────────────┘
```

### 5.2 The decomposition prompt

```python
import json
from dataclasses import dataclass


DECOMPOSITION_PROMPT = """\
You are a query decomposition engine. Break the user's complex question into simple,
self-contained sub-queries that can each be answered by searching a documentation corpus.

Rules:
1. Each sub-query must be independently searchable — no references to other sub-queries.
2. Each sub-query should target a DIFFERENT piece of information.
3. Preserve specific terms, names, and identifiers exactly.
4. Generate 2-5 sub-queries. Fewer is better if they cover the question.
5. Order sub-queries by dependency: if answering sub-query B requires knowing the answer
   to sub-query A, list A first.

Return a JSON array of objects, each with:
- "query": the sub-query text
- "purpose": one phrase describing what this sub-query retrieves
- "depends_on": list of indices (0-based) of sub-queries this one depends on, or []

User question: {query}

JSON:"""


@dataclass
class SubQuery:
    query: str
    purpose: str
    depends_on: list[int]
    index: int


async def decompose_query(
    query: str,
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
    max_sub_queries: int = 5,
) -> list[SubQuery]:
    """Decompose a complex query into independent sub-queries.
    
    Returns a list of SubQuery objects with dependency information.
    Falls back to the original query as a single sub-query on failure.
    """
    response = await llm_client.messages.create(
        model=model,
        max_tokens=500,
        temperature=0.0,
        messages=[{"role": "user", "content": DECOMPOSITION_PROMPT.format(query=query)}],
    )
    
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    
    try:
        parsed = json.loads(text)
        sub_queries = []
        for i, item in enumerate(parsed[:max_sub_queries]):
            sub_queries.append(SubQuery(
                query=item["query"],
                purpose=item.get("purpose", ""),
                depends_on=item.get("depends_on", []),
                index=i,
            ))
        return sub_queries
    except (json.JSONDecodeError, KeyError):
        return [SubQuery(query=query, purpose="original query", depends_on=[], index=0)]
```

### 5.3 Dependency graphs and execution order

Decomposition produces sub-queries that may have dependencies. "What is the retry policy?" must
be answered before "How does the retry policy interact with the circuit breaker?" because the
second sub-query's retrieval benefits from knowing what the retry policy actually is.

In practice, most decompositions produce independent sub-queries that can be retrieved in
parallel. True dependencies are rare and usually indicate that the query is better handled by
agentic multi-hop retrieval (`13`) than by static decomposition.

For the common case — independent sub-queries — the execution plan is simple:

```python
import asyncio
from typing import Any


async def retrieve_decomposed(
    sub_queries: list[SubQuery],
    retriever: Any,
    top_k_per_query: int = 10,
) -> dict[int, list[dict]]:
    """Retrieve for each sub-query, respecting dependencies.
    
    Independent sub-queries run in parallel.
    Dependent sub-queries wait for their dependencies (but we don't
    use dependency results to refine the query — that's agentic retrieval).
    """
    # Topological sort into layers
    layers: list[list[SubQuery]] = []
    resolved: set[int] = set()
    remaining = list(sub_queries)
    
    while remaining:
        layer = [sq for sq in remaining if all(d in resolved for d in sq.depends_on)]
        if not layer:
            # Circular dependency — just run everything
            layer = remaining
        layers.append(layer)
        for sq in layer:
            resolved.add(sq.index)
        remaining = [sq for sq in remaining if sq.index not in resolved]
    
    # Execute layers sequentially, queries within a layer in parallel
    results: dict[int, list[dict]] = {}
    for layer in layers:
        layer_results = await asyncio.gather(
            *[retriever.search(sq.query, top_k=top_k_per_query) for sq in layer]
        )
        for sq, res in zip(layer, layer_results):
            results[sq.index] = res
    
    return results
```

### 5.4 Fusion after decomposition

The sub-queries' results must be fused before reranking. This is `04` §5's fusion step with
additional branches. The design choice is whether to fuse all sub-query results into one pool
or to keep them separate and present them as grouped chunks to the generation model.

```
    Original query: "Compare auth mechanisms of A and B"
        │
        ├── sub-query 1: "service A authentication" ──► [chunks a1, a2, a3]
        │
        └── sub-query 2: "service B authentication" ──► [chunks b1, b2, b3]

    Option 1: Fuse all ──► RRF([a1,a2,a3,b1,b2,b3]) ──► rerank ──► top-k
    
    Option 2: Keep grouped ──► rerank per group ──► present as:
              "About A's auth: [a1, a2]"
              "About B's auth: [b1, b2]"
```

Option 2 is better for comparison queries because it preserves the structure the user asked for.
The generation model can produce a coherent comparison when the chunks are grouped by sub-query.
Option 1 is better when the sub-queries are not a comparison but rather different facets of the
same topic — the generation model does not need the grouping.

This is a routing decision (§2) for the decomposition strategy: the query's intent determines
how the sub-queries' results are organized for generation.

### 5.5 The over-decomposition problem

A common failure mode: the LLM decomposes a simple query into sub-queries that fragment the
retrieval signal rather than expanding it. "What is the timeout for API calls?" decomposed into
"What is a timeout?", "What types of API calls exist?", "What is the default configuration?" —
three sub-queries that are each less useful than the original.

The guard against over-decomposition:

1. **Complexity gating** (§2.2): only decompose queries classified as complex
2. **Minimum information gain**: each sub-query must target information *not present* in other
   sub-queries — the decomposition prompt's rule #2 enforces this
3. **Maximum sub-query count**: hard cap at 5, with 2-3 being the target
4. **The original query always runs** alongside sub-queries: over-decomposition adds noise but
   does not remove signal

---

## 6. Multi-query generation

Multi-query generation is related to but distinct from decomposition. Where decomposition breaks a
complex question into sub-questions about different topics, multi-query generates *different
phrasings* of the same question. The goal is not to cover different information needs but to cover
different vocabulary and embedding-space neighborhoods that might contain the same answer.

### 6.1 The diversity-redundancy tradeoff

Generating five paraphrases of "how does authentication work" gives you five shots at landing near
the relevant chunk in embedding space, but those five embeddings will be clustered together — the
marginal gain of each additional paraphrase diminishes rapidly. The value of multi-query is in
*diversity*, not volume.

Diversity means generating queries that:

- Use different vocabulary (synonyms, jargon variants)
- Approach the topic from different angles (user perspective vs. system perspective)
- Operate at slightly different granularity levels
- Target different embedding-space neighborhoods

```python
MULTI_QUERY_PROMPT = """\
Generate {n} diverse search queries that would help answer the user's question.
Each query should approach the topic from a different angle or use different terminology.

Rules:
1. Each query must seek the SAME information as the original question.
2. Use different vocabulary in each query — synonyms, related terms, domain jargon.
3. Vary the perspective: some queries from the user's viewpoint, some from the
   system/documentation viewpoint.
4. Do NOT just rephrase — each query should potentially match different documents.
5. Return one query per line, no numbering or bullets.

Original question: {query}

Diverse queries:"""


async def generate_multi_query(
    query: str,
    llm_client: Any,
    n: int = 3,
    model: str = "claude-sonnet-4-20250514",
) -> list[str]:
    """Generate diverse query variants for multi-query retrieval.
    
    Returns n query variants plus the original query.
    The original is always included as the first element.
    """
    response = await llm_client.messages.create(
        model=model,
        max_tokens=300,
        temperature=0.3,   # slight temperature for diversity
        messages=[
            {"role": "user", "content": MULTI_QUERY_PROMPT.format(n=n, query=query)}
        ],
    )
    
    lines = response.content[0].text.strip().split("\n")
    variants = [line.strip().lstrip("0123456789.-) ") for line in lines if line.strip()]
    variants = [v for v in variants if len(v) > 5][:n]
    
    # Original always first, deduplicated
    result = [query]
    seen = {query.lower()}
    for v in variants:
        if v.lower() not in seen:
            result.append(v)
            seen.add(v.lower())
    
    return result
```

### 6.2 Fusion for multi-query results

Multi-query results require a different fusion consideration than decomposition results. With
decomposition, each sub-query retrieves different chunks about different topics. With multi-query,
each variant retrieves *overlapping* sets of chunks about the same topic. The overlap is the signal
— a chunk retrieved by multiple variants is likely relevant.

This is where `04` §5's Reciprocal Rank Fusion (RRF) shines: a chunk that appears at rank 3 in
one variant's results and rank 5 in another's gets a higher fused score than a chunk that appears
at rank 1 in one variant and nowhere else. RRF is naturally a consensus metric.

```python
def reciprocal_rank_fusion(
    result_lists: list[list[dict]],
    k: int = 60,
    id_field: str = "chunk_id",
) -> list[dict]:
    """Fuse multiple result lists using Reciprocal Rank Fusion.
    
    Args:
        result_lists: each is a ranked list of chunks from one query variant
        k: RRF constant (higher = less top-rank-dominant)
        id_field: field name for deduplication
    
    Returns:
        Fused list sorted by RRF score, descending.
    """
    scores: dict[str, float] = {}
    chunks: dict[str, dict] = {}
    
    for results in result_lists:
        for rank, chunk in enumerate(results, start=1):
            chunk_id = chunk[id_field]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            if chunk_id not in chunks:
                chunks[chunk_id] = chunk
    
    # Sort by RRF score descending
    ranked_ids = sorted(scores, key=scores.__getitem__, reverse=True)
    return [
        {**chunks[cid], "_rrf_score": scores[cid]}
        for cid in ranked_ids
    ]
```

### 6.3 How many variants are enough?

Empirically, on most corpora, 3-4 variants provide most of the recall gain. The marginal
improvement from the 4th and 5th variants is measurably smaller than the improvement from the
2nd and 3rd. Beyond 5, the cost dominates the gain unless you have an unusually large vocabulary
mismatch.

This is measurable on your corpus in under an hour: generate 1 through 6 variants for your eval
set, measure recall@k for each count, and plot the curve. You will find a knee, and that knee is
your operating point.

```
    recall@20 vs number of query variants (typical)

    0.90 │                         ●───────●
         │                    ●────┘
    0.85 │               ●────┘
         │          ●────┘
    0.80 │     ●────┘
         │    ┌┘
    0.75 │────┘
         │
    0.70 │●
         └───┬───┬───┬───┬───┬───┬───
             1   2   3   4   5   6
                 query variants

    The knee is typically at 3. The 4th variant adds ~1 pp.
    The 6th variant adds ~0.2 pp and costs as much as the 1st.
```

### 6.4 Multi-query versus rewriting

Multi-query and rewriting solve the same problem (vocabulary mismatch) but with different
mechanisms. Rewriting produces one better query. Multi-query produces several diverse queries.
In practice:

- **Rewriting is cheaper** (one LLM call) and sufficient when the vocabulary mismatch is
  moderate — the rewrite can bridge the gap on its own
- **Multi-query is more robust** when you do not know which vocabulary the relevant chunks use —
  multiple diverse variants are more likely to cover the right terms
- **The combination** (rewrite first, then generate variants of the rewritten query) is rarely
  worth the cost — the variants already cover what the rewrite would have produced

The routing decision (§2.4) chooses between them based on classification confidence and latency
budget.

---

## 7. HyDE — Hypothetical Document Embeddings

HyDE is a conceptually different strategy from rewriting and multi-query. Instead of manufacturing
a better *query*, it manufactures a hypothetical *answer* and uses that answer's embedding for
retrieval. The insight: **the embedding of a plausible answer is closer to the embedding of the
actual answer-containing chunk than the embedding of the question is.**

### 7.1 Why HyDE works

Consider the embedding space geometry. A question and its answer are semantically related but
structurally different. "What is the retry backoff strategy?" lives in question-space. "The system
uses exponential backoff starting at 100ms with a 2x multiplier and a maximum of 30 seconds" lives
in answer-space. In a symmetric embedding model, these two texts may not be nearest neighbors —
the answer is more similar to other answers about retry strategies than it is to the question.

HyDE exploits this by generating a hypothetical answer — which does not need to be factually
correct — and embedding it. The hypothetical answer "The retry backoff strategy uses exponential
backoff with configurable initial delay and maximum retry count" is syntactically and topically
similar to the actual answer chunk, so its embedding is *closer* to the actual answer in embedding
space than the question's embedding would be.

```
    Embedding space (conceptual 2D projection):

                            ● actual answer chunk
                           ╱
                    ● HyDE embedding (closer!)
                   ╱
                  ╱
    ● question embedding (farther)


    distance(HyDE_embedding, actual_answer) < distance(question_embedding, actual_answer)
```

This is `01`'s asymmetric embedding problem in reverse. If the embedding model has an
`input_type=query` / `input_type=document` distinction, the question embedding is placed in
query space and the chunk embedding is in document space, and the model was trained to bring them
together. But when the model is symmetric, or when the asymmetry is insufficient to bridge the
vocabulary gap, HyDE can help by projecting the query *into document space* via a generated
document.

### 7.2 The implementation

```python
HYDE_PROMPT = """\
Write a short paragraph that would appear in technical documentation as the answer to the
following question. The answer should be specific and technical, using the terminology that
documentation would use.

IMPORTANT: Write what you think the answer WOULD look like, even if you are not certain of
the specific details. The goal is to generate text in the STYLE and VOCABULARY of the answer,
not to be factually correct.

Question: {query}

Documentation paragraph:"""


async def generate_hypothetical_document(
    query: str,
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
    n: int = 1,
) -> list[str]:
    """Generate hypothetical document(s) for HyDE retrieval.
    
    Args:
        query: the user's question
        n: number of hypothetical documents to generate
           (multiple increases coverage at embedding cost)
    
    Returns:
        List of hypothetical document texts.
    """
    responses = []
    for _ in range(n):
        response = await llm_client.messages.create(
            model=model,
            max_tokens=300,
            temperature=0.4 if n > 1 else 0.0,  # temperature for diversity
            messages=[
                {"role": "user", "content": HYDE_PROMPT.format(query=query)}
            ],
        )
        responses.append(response.content[0].text.strip())
    
    return responses


async def hyde_retrieve(
    query: str,
    llm_client: Any,
    embedding_model: Any,
    vector_store: Any,
    top_k: int = 20,
    n_hypothetical: int = 1,
) -> list[dict]:
    """Full HyDE retrieval pipeline.
    
    1. Generate hypothetical document(s)
    2. Embed the hypothetical document(s)
    3. Retrieve using the hypothetical embedding(s)
    4. Also retrieve using the original query
    5. Fuse all results
    """
    import asyncio
    
    # Generate hypothetical documents
    hypothetical_docs = await generate_hypothetical_document(
        query, llm_client, n=n_hypothetical,
    )
    
    # Embed everything in parallel
    texts_to_embed = [query] + hypothetical_docs
    embeddings = await embedding_model.embed_batch(
        texts_to_embed,
        input_type="search_query",  # 01 §3
    )
    
    # Retrieve for each embedding in parallel
    retrieval_tasks = [
        vector_store.search_by_vector(emb, top_k=top_k)
        for emb in embeddings
    ]
    all_results = await asyncio.gather(*retrieval_tasks)
    
    # Fuse
    return reciprocal_rank_fusion(all_results, k=60)
```

### 7.3 When HyDE helps

HyDE helps for a specific and measurable class of queries: those where the **vocabulary mismatch
is between the question frame and the answer frame**, rather than between synonyms. This includes:

| Query type | Why HyDE helps | Example |
|---|---|---|
| Troubleshooting | user describes symptom, corpus describes solution | "my app is slow" vs. "optimize database connection pooling" |
| Conceptual | user asks "what is X", corpus explains X in its own terms | "what is backpressure" vs. "flow control in reactive streams" |
| How-to | user asks how, corpus documents the procedure | "how to deploy" vs. "deployment pipeline configuration guide" |

### 7.4 When HyDE hurts

HyDE can actively degrade retrieval in several cases:

1. **Factual lookups.** "What is the default value of max_connections?" — the hypothetical answer
   will fabricate a number, and the embedding of "the default value of max_connections is 100" is
   farther from the chunk that says "the default is 50" than the original question's embedding.
   HyDE's factual incorrectness pulls the embedding toward the wrong neighborhood.

2. **Already-precise queries.** "Kubernetes pod eviction due to ephemeral storage exceeding limit"
   — this query already uses the corpus's vocabulary. HyDE's hypothetical answer is a
   paraphrase that can only dilute.

3. **Identifier-heavy queries.** "Error ERR_CONN_REFUSED on service gateway-proxy-v2" — the
   hypothetical answer will not reproduce the identifier exactly, and the embedding loses the
   one signal that matters.

4. **When the LLM's knowledge is wrong about the domain.** If the corpus documents an internal
   system that the LLM has never seen, the hypothetical answer will describe a generic system,
   and its embedding will retrieve generic chunks rather than the domain-specific ones.

The last point is the most important operationally: **HyDE works best when the LLM's parametric
knowledge is a reasonable prior for the corpus's content, and worst when the corpus is novel to
the LLM.** For public documentation of popular technologies, HyDE is strong. For internal
proprietary documentation, it is often harmful.

### 7.5 HyDE with multiple hypothetical documents

Generating multiple hypothetical documents (n > 1, with temperature > 0) and averaging their
embeddings is a variance reduction technique. Each hypothetical answer captures slightly different
aspects of the answer space, and the average embedding is closer to the centroid of the relevant
region.

```python
import numpy as np


def average_embeddings(embeddings: list[list[float]]) -> list[float]:
    """Average multiple embeddings and re-normalize to unit length.
    
    This is the mean of the HyDE embeddings, projected back onto the
    unit sphere (for cosine similarity search).
    """
    avg = np.mean(embeddings, axis=0)
    norm = np.linalg.norm(avg)
    if norm < 1e-10:
        return embeddings[0]  # degenerate case
    return (avg / norm).tolist()
```

The tradeoff: each additional hypothetical document costs one LLM call (generation) plus one
embedding call. Two hypothetical documents are often worthwhile; more than three rarely are.
Measure on your corpus — the marginal recall gain diminishes as in §6.3's curve, but the
starting point is different because HyDE operates in a different part of the gap space.

### 7.6 HyDE for different embedding models

HyDE's effectiveness depends on the embedding model's geometry. Models with strong asymmetric
training (`01` §3) — where query and document embeddings are projected into the same space
despite their structural differences — reduce the need for HyDE. The better the model handles
the question-answer asymmetry natively, the less HyDE can add.

Conversely, symmetric models (or models used without the `input_type` distinction) benefit most
from HyDE, because the structural difference between questions and answers is fully reflected in
the embedding distance.

This is why HyDE should always be evaluated *in combination with your specific embedding model*
rather than adopted from a paper that used a different model. The paper's positive result may not
transfer.

---

## 8. Query expansion — classical and modern

Query expansion adds terms to the query to increase vocabulary coverage. It is the oldest technique
in this chapter — predating neural approaches by decades — and it remains useful because the
problem it solves (the query does not contain the terms the relevant documents contain) is
fundamental.

### 8.1 Classical: pseudo-relevance feedback (PRF)

PRF assumes that the top-k results from the initial retrieval are *mostly relevant* (the "pseudo"
in pseudo-relevance). It extracts terms from those top-k results and adds them to the query,
then re-retrieves with the expanded query.

```
    original query ──► first retrieval ──► top-k results
                                                │
                                          extract terms
                                                │
                                                ▼
    expanded query ◄─── original + extracted ──► second retrieval ──► final results
```

The Rocchio algorithm formalizes this:

```
    q_expanded = α · q_original + β · mean(relevant_docs) - γ · mean(non_relevant_docs)
```

In the pseudo-relevance setting, `relevant_docs` is the top-k from the first retrieval and
`non_relevant_docs` is either empty or the bottom-ranked results. Typical values are
`α = 1.0`, `β = 0.75`, `γ = 0.15`.

In an embedding-based system, Rocchio operates on the embedding vectors:

```python
import numpy as np
from typing import Optional


def rocchio_expansion(
    query_vector: np.ndarray,
    relevant_vectors: list[np.ndarray],
    non_relevant_vectors: Optional[list[np.ndarray]] = None,
    alpha: float = 1.0,
    beta: float = 0.75,
    gamma: float = 0.15,
) -> np.ndarray:
    """Rocchio expansion in embedding space.
    
    Shifts the query vector toward the centroid of the relevant documents
    and away from the non-relevant documents.
    
    Args:
        query_vector: original query embedding
        relevant_vectors: embeddings of pseudo-relevant documents (top-k results)
        non_relevant_vectors: embeddings of non-relevant documents (optional)
        alpha, beta, gamma: Rocchio coefficients
    
    Returns:
        Expanded query vector, normalized to unit length.
    """
    expanded = alpha * query_vector
    
    if relevant_vectors:
        rel_centroid = np.mean(relevant_vectors, axis=0)
        expanded += beta * rel_centroid
    
    if non_relevant_vectors:
        non_rel_centroid = np.mean(non_relevant_vectors, axis=0)
        expanded -= gamma * non_rel_centroid
    
    norm = np.linalg.norm(expanded)
    if norm < 1e-10:
        return query_vector
    return expanded / norm
```

### 8.2 The PRF failure mode

PRF has a well-known failure mode called **query drift**: if the initial retrieval returns
irrelevant documents (precisely the situation where expansion would help most), the extracted
terms are noise, and the expanded query drifts further from relevance. PRF helps queries that
were already working reasonably well and hurts queries that were failing.

This is a fundamental tension: the technique that adds the most terms (PRF with high `β`) also
drifts the most when the initial results are bad. Conservative parameter choices (`β ≤ 0.5`) reduce
drift but also reduce the expansion's usefulness.

### 8.3 Modern: LLM-based expansion

LLMs can replace PRF's extract-from-results step with generate-from-knowledge:

```python
EXPANSION_PROMPT = """\
Given the following search query, generate a list of related technical terms, synonyms,
and concepts that would help find relevant documentation.

Rules:
1. Include synonyms and alternative phrasings of key concepts
2. Include related technical terms that documentation might use
3. Include acronyms and their expansions
4. Do NOT include terms that would broaden the search beyond the user's intent
5. Return 5-10 terms, one per line

Query: {query}

Related terms:"""


async def expand_query_llm(
    query: str,
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Expand query with LLM-generated related terms.
    
    Returns the original query with expansion terms appended.
    The expanded query is used for the lexical branch; the original
    query is used for the dense branch (expansion terms hurt dense
    retrieval by diluting the embedding).
    """
    response = await llm_client.messages.create(
        model=model,
        max_tokens=200,
        temperature=0.0,
        messages=[{"role": "user", "content": EXPANSION_PROMPT.format(query=query)}],
    )
    
    terms = response.content[0].text.strip().split("\n")
    terms = [t.strip().lstrip("0123456789.-) ") for t in terms if t.strip()]
    
    # Append expansion terms to the original query
    expansion = " ".join(terms[:8])
    return f"{query} {expansion}"
```

### 8.4 Expansion is primarily a lexical technique

A critical point that the LLM-expansion tutorials miss: **expansion terms improve lexical
retrieval (BM25) more than dense retrieval.** For BM25, adding "exponential backoff" to a query
about "retry strategy" creates a term match where none existed. For dense retrieval, the
additional terms dilute the embedding — the embedding of "retry strategy exponential backoff
jitter maximum delay" is not necessarily closer to the relevant chunk than "retry strategy" alone.

The practical implication: generate expansion terms for the BM25 branch specifically, and keep the
original (or rewritten) query for the dense branch. This is a branch-specific manufacturing
strategy, not a global one.

```
    raw query
        │
        ├──────────────────────────── dense branch: use original or rewritten query
        │
        └── expand (§8.3) ──────────► lexical branch: use expanded query
```

### 8.5 Expansion versus rewriting

Expansion adds terms without removing any. Rewriting replaces the entire query. The distinction
matters:

| Property | Expansion | Rewriting |
|---|---|---|
| Mechanism | append terms | replace query |
| Risk of losing intent | low (original terms preserved) | moderate (rewriter may drop important terms) |
| Vocabulary coverage | increases monotonically | may increase or decrease |
| Embedding impact | dilutes (bad for dense) | refocuses (can help or hurt dense) |
| Best branch | lexical (BM25) | dense (semantic) |
| Cost | low (small output) | low (small output) |

The two are complementary and can run together: rewrite for dense, expand for lexical, as shown in
§12's pipeline.

---

## 9. Conversational query resolution

In a multi-turn conversation, the user's query is often incomplete — it contains pronouns,
ellipsis, or implicit references that make sense only in the context of prior turns. "What about
the pricing?" is meaningless in isolation; with the prior turn "Tell me about service X," it means
"What is the pricing of service X?"

### 9.1 The coreference problem

Coreference resolution in the conversational RAG setting is simpler than general linguistic
coreference resolution because the referents are almost always in the chat history, not in the
document. The task is: given the chat history and the current query, produce a **standalone query**
that can be used for retrieval without any conversation context.

```python
COREFERENCE_PROMPT = """\
Given the following conversation history and the user's latest message, rewrite the latest
message as a STANDALONE search query that contains all necessary context.

Rules:
1. Replace all pronouns (it, this, that, they, etc.) with their referents from the history.
2. Include any context from the history that is needed to understand the query.
3. Do NOT include information the user did not ask about.
4. Do NOT answer the question — just make it self-contained.
5. If the latest message is already self-contained, return it unchanged.
6. Output ONLY the standalone query.

Conversation history:
{history}

User's latest message: {query}

Standalone query:"""


def format_chat_history(
    messages: list[dict],
    max_turns: int = 5,
    max_chars: int = 2000,
) -> str:
    """Format recent chat history for the coreference prompt.
    
    Truncates to the most recent turns to control prompt size.
    Only includes enough context for coreference resolution.
    """
    recent = messages[-max_turns * 2:]  # user + assistant pairs
    
    lines = []
    total_chars = 0
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        
        # Truncate long assistant responses
        if role == "assistant" and len(content) > 200:
            content = content[:200] + "..."
        
        line = f"{role.capitalize()}: {content}"
        total_chars += len(line)
        if total_chars > max_chars:
            break
        lines.append(line)
    
    return "\n".join(lines)


async def resolve_coreferences(
    query: str,
    chat_history: list[dict],
    llm_client: Any,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Resolve coreferences to produce a standalone query.
    
    This is the FIRST step in query understanding for conversational
    contexts.  All other strategies operate on the resolved query.
    """
    if not chat_history:
        return query
    
    history_str = format_chat_history(chat_history)
    prompt = COREFERENCE_PROMPT.format(history=history_str, query=query)
    
    response = await llm_client.messages.create(
        model=model,
        max_tokens=200,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    
    resolved = response.content[0].text.strip()
    
    # Safety: if the resolved query is dramatically different from the original,
    # something went wrong.  Keep both.
    if len(resolved) > len(query) * 4:
        return query
    
    return resolved
```

### 9.2 When to resolve

Coreference resolution should run **before** all other query understanding strategies, because
every downstream strategy (rewriting, decomposition, HyDE) needs a complete query to work with.
A decomposition of "What about the pricing?" produces nonsense; a decomposition of "What is the
pricing model of AWS Lambda?" produces useful sub-queries.

```
    conversational query
         │
         ▼
    ┌─────────────────┐
    │  resolve (§9)   │  if chat_history is non-empty
    └────────┬────────┘
             │
             ▼
    standalone query
         │
         ▼
    ┌─────────────────┐
    │  classify (§2)  │
    └────────┬────────┘
             │
             ▼
    [rewrite / decompose / HyDE / expand]
```

### 9.3 Context window management for history

The coreference prompt includes chat history, which consumes tokens. For a long conversation, the
full history is too large and mostly irrelevant — the referent is almost always in the last 2-3
turns. The `format_chat_history` function above truncates to the most recent turns, but a more
sophisticated approach is to include only the turns that contain potential referents:

```python
def select_relevant_history(messages: list[dict], query: str, max_turns: int = 3) -> list[dict]:
    """Include the most recent turns; add more when the query has pronouns."""
    recent = messages[-2:] if len(messages) >= 2 else messages[:]
    if re.search(r'\b(it|this|that|these|those|its|they|them|their)\b', query.lower()) and len(messages) > 2:
        recent = messages[-(max_turns * 2):-2] + recent
    return recent[-max_turns * 2:]
```

### 9.4 Coreference as an LLM call cost

Coreference resolution adds one LLM call to every conversational query. This is frequently worth
it — a query with unresolved pronouns will fail retrieval — but the cost is non-trivial in
high-volume conversational systems.

Two optimizations:

1. **Skip resolution when not needed.** The heuristic classifier (§2.2) can detect whether the
   query contains pronouns or is a short follow-up. If neither, skip the LLM call.
2. **Combine resolution with rewriting.** A single LLM call can both resolve coreferences and
   rewrite for retrieval, saving one round-trip:

```python
COMBINED_PROMPT = """\
Given the conversation history and the user's latest message:
1. Replace all pronouns and references with their concrete referents from the history.
2. Rewrite the result as an optimal search query for technical documentation.
3. Output ONLY the final search query.

History:
{history}

Latest message: {query}

Search query:"""
```

This combined prompt is the pragmatic choice for conversational RAG systems that also need
rewriting. It trades the modularity of separate resolution and rewriting steps for the latency
savings of one fewer LLM call. The tradeoff is worth it when your latency budget is tight (§10).

---

## 10. The cost model for query understanding

Every technique in this chapter adds at least one LLM call. Each LLM call adds latency, cost,
and a failure mode. The cost model must account for all three.

### 10.1 Latency arithmetic

```
    total_latency = max(
        latency(resolution) +         # serial: must complete before anything else
        latency(classification) +     # serial: determines what runs next
        max(                           # parallel: strategies run concurrently
            latency(rewrite),
            latency(decompose),
            latency(hyde),
            latency(expand),
        ) +
        latency(retrieval) +          # serial: waits for manufactured queries
        latency(rerank) +             # serial: waits for retrieval
        latency(generation)           # serial: waits for everything
    )
```

In practice, with a hosted LLM API:

| Step | Typical p50 (ms) | Typical p99 (ms) | Notes |
|---|---:|---:|---|
| Coreference resolution | 300 | 800 | one LLM call, short output |
| Query classification (heuristic) | 1 | 5 | local, no LLM |
| Query classification (LLM) | 250 | 700 | one LLM call, short output |
| Query rewrite | 300 | 800 | one LLM call, short output |
| Query decomposition | 400 | 1000 | one LLM call, longer output |
| HyDE generation | 500 | 1200 | one LLM call, paragraph output |
| Multi-query generation | 400 | 1000 | one LLM call, multi-line output |
| Query expansion (LLM) | 250 | 600 | one LLM call, short output |
| Query expansion (PRF) | 50 | 100 | no LLM, just initial retrieval |

These are ballpark figures for a modern hosted API (Claude, GPT-4o, etc.) at typical load.
Your numbers will differ and you should measure them (`10`).

### 10.2 The serial cost of query understanding

The critical observation: **query understanding adds latency *serially* before retrieval.**
Retrieval cannot begin until the manufactured queries are ready. In a pipeline where retrieval
takes 50ms and reranking takes 100ms, adding a 500ms query rewrite step more than triples the
total latency.

```
    Without query understanding:
    ├─ retrieval (50ms) ─┤─ rerank (100ms) ─┤─ generation (800ms) ─┤
    total: ~950ms

    With rewrite + HyDE:
    ├─ rewrite (300ms) ─┤─ hyde (500ms) ─┤─ retrieval (50ms) ─┤─ rerank (100ms) ─┤─ gen (800ms) ─┤
    total: ~1750ms

    With rewrite + HyDE (parallelized):
    ├─ max(rewrite, hyde) = 500ms ─┤─ retrieval (50ms) ─┤─ rerank (100ms) ─┤─ gen (800ms) ─┤
    total: ~1450ms
```

Parallelizing the strategies helps but does not eliminate the serial cost. The p99 is determined
by the slowest strategy, and LLM p99s are 2-3x the p50.

### 10.3 Cost per query

Dollar cost for LLM-based query understanding on hosted APIs (order of magnitude):

```
    Input tokens per query understanding call: ~200-500 (prompt + query)
    Output tokens per call: ~50-200 (rewritten query or classification)
    Cost per call at typical pricing: ~$0.0005 - $0.003

    Strategies applied per query (typical):
      Simple query (passthrough): 0 LLM calls
      Moderate query (rewrite): 1 call                = ~$0.001
      Complex query (decompose + rewrite): 2 calls    = ~$0.003
      HyDE: 1 call                                     = ~$0.002
      Full pipeline: 3-4 calls                         = ~$0.005-0.010

    At 100,000 queries/day:
      Passthrough: $0
      Moderate: $100/day
      Complex: $300/day
      Full pipeline on every query: $500-1000/day
```

These costs are small relative to the generation call ($0.01-0.10 per query) but not trivial at
scale. More importantly, they are *wasted* on queries that would have retrieved well without
manufacturing — which is why routing (§2) is the most important optimization.

### 10.4 When to skip query understanding

The cost model leads to a clear decision framework:

1. **Always skip** for queries classified as simple with high confidence (§2.2). The heuristic
   classifier costs ~1ms. The LLM classifier costs ~300ms. The heuristic should handle the
   easy cases.
2. **Rewrite only** for moderate queries with vocabulary mismatch signals. One LLM call, ~300ms.
3. **Decompose** only for queries with multiple retrieval needs. One LLM call, ~400ms.
4. **HyDE** only for troubleshooting and conceptual queries where the answer-question gap is
   large. One LLM call, ~500ms.
5. **Never run all strategies on every query.** That is the anti-pattern this chapter exists to
   prevent.

### 10.5 The latency budget as a constraint on strategy selection

The routing table (§2.4) should enforce a latency budget. If the total budget is 2 seconds and
generation takes 800ms and retrieval + reranking takes 200ms, the query understanding budget is
1 second. That is enough for *one* LLM-based strategy (with margin for the p99) or *two*
strategies parallelized.

```python
def select_strategies_within_budget(
    candidates: list[str], latency_estimates_ms: dict[str, float],
    budget_ms: float, retrieval_ms: float = 200.0, generation_ms: float = 800.0,
) -> list[str]:
    """Select strategies that fit the latency budget (parallel execution: cost = max)."""
    available = budget_ms - retrieval_ms - generation_ms
    if available <= 0:
        return []
    priority = ["rewrite", "hyde", "decompose", "step_back", "expand", "multi_query"]
    selected: list[str] = []
    max_lat = 0.0
    for s in priority:
        if s in candidates:
            new_max = max(max_lat, latency_estimates_ms.get(s, 500.0))
            if new_max <= available:
                selected.append(s)
                max_lat = new_max
    return selected
```

---

## 11. Caching query transformations

If the same query (or a semantically similar query) is asked repeatedly, the LLM call to rewrite
it should not be repeated. Caching query transformations amortizes the latency and cost of query
understanding across repeated queries.

### 11.1 Exact-match cache

The simplest cache: hash the raw query (after normalization), store the manufactured queries keyed
by that hash.

```python
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class CachedTransformation:
    original_query: str
    manufactured_queries: list[str]
    strategy_applied: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    hit_count: int = 0


class QueryTransformationCache:
    """LRU cache keyed by normalized query text, with TTL expiration."""
    
    def __init__(self, max_size: int = 10_000, ttl_hours: int = 24):
        self._cache: dict[str, CachedTransformation] = {}
        self._max_size = max_size
        self._ttl = timedelta(hours=ttl_hours)
    
    def _key(self, query: str) -> str:
        return hashlib.sha256(query.lower().strip().encode()).hexdigest()
    
    def get(self, query: str) -> Optional[CachedTransformation]:
        entry = self._cache.get(self._key(query))
        if entry is None:
            return None
        if datetime.utcnow() - entry.created_at > self._ttl:
            del self._cache[self._key(query)]
            return None
        entry.hit_count += 1
        return entry
    
    def put(self, query: str, manufactured_queries: list[str], strategy: str) -> None:
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k].created_at)
            del self._cache[oldest]
        self._cache[self._key(query)] = CachedTransformation(
            query, manufactured_queries, strategy,
        )
```

### 11.2 Semantic cache

Exact-match caching misses the case where two queries are semantically identical but lexically
different — "how to deploy a container" and "deploying a container how to" should share a cached
transformation. Semantic caching embeds the query and searches for a cached transformation whose
query embedding is within a cosine-similarity threshold.

```python
import numpy as np


class SemanticQueryCache:
    """Cache with embedding-similarity lookup.
    
    Threshold governs hit quality:
      < 0.90 — false hits (different queries share cached rewrites)
      > 0.98 — too few hits (cache is useless)
      0.92-0.95 — sweet spot for most embedding models
    """
    
    def __init__(self, embedding_model: Any, max_size: int = 10_000,
                 similarity_threshold: float = 0.93, ttl_hours: int = 24):
        self._embedding_model = embedding_model
        self._max_size = max_size
        self._threshold = similarity_threshold
        self._ttl = timedelta(hours=ttl_hours)
        self._entries: list[CachedTransformation] = []
        self._embeddings: list[np.ndarray] = []
    
    async def get(self, query: str) -> Optional[CachedTransformation]:
        if not self._entries:
            return None
        query_vec = np.array(await self._embedding_model.embed(query, input_type="search_query"))
        sims = np.array([np.dot(query_vec, e) / (np.linalg.norm(query_vec) * np.linalg.norm(e))
                         for e in self._embeddings])
        best = int(np.argmax(sims))
        if sims[best] >= self._threshold:
            entry = self._entries[best]
            if datetime.utcnow() - entry.created_at <= self._ttl:
                entry.hit_count += 1
                return entry
        return None
    
    async def put(self, query: str, manufactured_queries: list[str], strategy: str) -> None:
        emb = np.array(await self._embedding_model.embed(query, input_type="search_query"))
        if len(self._entries) >= self._max_size:
            self._entries.pop(0)
            self._embeddings.pop(0)
        self._entries.append(CachedTransformation(query, manufactured_queries, strategy))
        self._embeddings.append(emb)
```

### 11.3 Cache invalidation

The cache must be invalidated when the corpus changes. A rewrite that was optimal for the old
corpus may be suboptimal for the new one — new documents may use different vocabulary. The TTL
parameter handles this coarsely; a more precise approach is to invalidate the cache on every
corpus update (reindex event from `03`).

In practice, a 24-hour TTL is a reasonable default for corpora that update daily. For
rapidly-changing corpora, use a shorter TTL or couple invalidation to the ingest pipeline.

### 11.4 What to cache versus what to recompute

Not all query understanding steps are worth caching:

| Step | Cache? | Reasoning |
|---|---|---|
| Coreference resolution | No | depends on chat history, which differs per conversation |
| Classification | Yes | deterministic for the same query |
| Rewriting | Yes | deterministic (temperature=0) |
| Decomposition | Yes | deterministic |
| HyDE generation | Maybe | useful at temperature=0; at temperature>0, regenerating produces diversity |
| Multi-query generation | Maybe | if temperature>0, regenerating is the point |
| Expansion (LLM) | Yes | deterministic |
| Expansion (PRF) | No | depends on current retrieval results, which change with the corpus |

---

## 12. Combining techniques: the query preprocessing pipeline

Individual techniques are building blocks. The query preprocessing pipeline is the architecture
that combines them into a coherent system. The design questions are: what order, what conditions,
and what fallbacks.

### 12.1 The canonical pipeline

```
    raw query + chat history (optional)
        │
        ▼
    ┌─────────────────────────────┐
    │  1. Coreference resolution  │  conditional: only if chat_history
    │     (§9)                    │  is non-empty AND query has pronouns
    └──────────┬──────────────────┘
               │
               ▼
    standalone query
        │
        ▼
    ┌─────────────────────────────┐
    │  2. Cache lookup (§11)      │  if hit: skip to retrieval
    └──────────┬──────────────────┘
               │ cache miss
               ▼
    ┌─────────────────────────────┐
    │  3. Classification (§2)     │  heuristic first;
    │     + routing               │  LLM if heuristic is uncertain
    └──────────┬──────────────────┘
               │ routing decision
               ▼
    ┌─────────────────────────────────────────────────────────┐
    │  4. Manufacturing (parallel where possible)             │
    │                                                         │
    │  ┌─────────┐  ┌───────────┐  ┌──────┐  ┌────────┐      │
    │  │ rewrite │  │ decompose │  │ HyDE │  │ expand │      │
    │  │  (§3)   │  │   (§5)    │  │ (§7) │  │  (§8)  │      │
    │  └────┬────┘  └─────┬─────┘  └──┬───┘  └───┬────┘      │
    │       │             │           │          │            │
    │       ▼             ▼           ▼          ▼            │
    │  ┌─────────────────────────────────────────────────┐    │
    │  │         manufactured retrieval inputs            │    │
    │  └──────────────────┬──────────────────────────────┘    │
    └─────────────────────┼───────────────────────────────────┘
                          │
                          ▼
    ┌─────────────────────────────┐
    │  5. Cache store (§11)       │  save manufactured queries
    └──────────┬──────────────────┘
               │
               ▼
    [original query + manufactured queries] ──► retrieval cascade (04)
```

### 12.2 The implementation

```python
import asyncio, time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QueryUnderstandingResult:
    original_query: str
    resolved_query: str
    manufactured_queries: list[str]
    hyde_embeddings: list[list[float]]
    classification: dict
    strategies_applied: list[str]
    latency_ms: float = 0.0
    cache_hit: bool = False
    
    @property
    def all_retrieval_queries(self) -> list[str]:
        seen = {self.resolved_query.lower()}
        out = [self.resolved_query]
        for q in self.manufactured_queries:
            if q.lower() not in seen:
                out.append(q); seen.add(q.lower())
        return out


class QueryUnderstandingPipeline:
    def __init__(self, llm_client: Any, embedding_model: Any,
                 cache: Optional[QueryTransformationCache] = None,
                 model: str = "claude-sonnet-4-20250514", latency_budget_ms: float = 2000.0):
        self._llm = llm_client
        self._emb = embedding_model
        self._cache = cache or QueryTransformationCache()
        self._model = model
        self._budget = latency_budget_ms
    
    async def process(self, query: str, chat_history: Optional[list[dict]] = None) -> QueryUnderstandingResult:
        t0 = time.monotonic()
        r = QueryUnderstandingResult(query, query, [], [], {}, [])
        
        # 1. Coreference resolution
        if chat_history and classify_query_heuristic(query, True).needs_resolution:
            r.resolved_query = await resolve_coreferences(query, chat_history, self._llm, self._model)
            r.strategies_applied.append("coreference_resolution")
        
        # 2. Cache
        cached = self._cache.get(r.resolved_query)
        if cached:
            r.manufactured_queries = cached.manufactured_queries
            r.cache_hit = True
            r.latency_ms = (time.monotonic() - t0) * 1000
            return r
        
        # 3. Classify + route
        cls = classify_query_heuristic(r.resolved_query)
        if cls.confidence < 0.6:
            r.classification = await classify_query_llm(r.resolved_query, self._llm, self._model)
        else:
            r.classification = {"complexity": cls.complexity.value, "intent": cls.intent,
                                "strategy": _heuristic_to_strategies(cls)}
        routing = route(r.classification, self._budget)
        
        # 4. Manufacturing (parallel, budget-gated)
        dispatch = {
            "rewrite":   (routing.rewrite,   rewrite_query),
            "decompose": (routing.decompose, decompose_query),
            "hyde":      (routing.hyde,       generate_hypothetical_document),
            "expand":    (routing.expand,     expand_query_llm),
            "step_back": (routing.step_back,  generate_step_back_query),
        }
        tasks = {name: asyncio.create_task(fn(r.resolved_query, self._llm, self._model))
                 for name, (enabled, fn) in dispatch.items() if enabled}
        if tasks:
            remaining_s = max(0.1, (self._budget - (time.monotonic() - t0) * 1000) / 1000)
            await asyncio.wait(tasks.values(), timeout=remaining_s)
        
        for name, task in tasks.items():
            if not (task.done() and not task.cancelled()):
                continue
            try:
                res = task.result()
            except Exception:
                continue
            r.strategies_applied.append(name)
            if name == "decompose" and isinstance(res, list):
                r.manufactured_queries.extend(sq.query for sq in res)
            elif isinstance(res, list):          # HyDE returns list[str]
                r.manufactured_queries.extend(res)
            elif isinstance(res, str):
                r.manufactured_queries.append(res)
        
        # 5. Cache store
        self._cache.put(r.resolved_query, r.manufactured_queries,
                        "+".join(sorted(r.strategies_applied)))
        r.latency_ms = (time.monotonic() - t0) * 1000
        return r


def _heuristic_to_strategies(c: ClassificationResult) -> list[str]:
    match c.complexity:
        case QueryComplexity.SIMPLE:
            return ["hyde"] if c.intent == "troubleshooting" else ["passthrough"]
        case QueryComplexity.MODERATE:
            return ["rewrite", "hyde"] if c.intent == "troubleshooting" else ["rewrite"]
        case QueryComplexity.COMPLEX:
            return ["decompose"] if c.intent == "comparison" else ["decompose", "rewrite"]
        case QueryComplexity.CONVERSATIONAL:
            return ["rewrite"]
    return ["passthrough"]
```

### 12.3 Ordering constraints

The pipeline has strict ordering constraints:

1. **Coreference resolution must be first.** All other strategies need a standalone query.
2. **Classification must precede manufacturing.** The routing decision determines which
   strategies run.
3. **Manufacturing strategies are parallel.** They are independent — rewriting does not need
   decomposition's output, and HyDE does not need the rewrite.
4. **Cache check is before classification.** If the cache has the manufactured queries, skip
   the LLM calls entirely.
5. **The original query always reaches retrieval.** No manufacturing step replaces it; all are
   additive branches.

### 12.4 Fallback chains

When an LLM call fails (timeout, rate limit, error), the pipeline must not fail. The fallback
is always the same: use the original (or resolved) query. Manufacturing is optional enhancement;
retrieval with the original query is the baseline that must always work.

```python
async def manufacture_with_fallback(
    strategy_fn: Any,
    query: str,
    timeout_ms: float = 1000.0,
    **kwargs: Any,
) -> Optional[str]:
    """Run a manufacturing strategy with timeout and error fallback.
    
    Returns None on any failure — the caller includes the original
    query regardless, so None means "this strategy didn't contribute."
    """
    try:
        result = await asyncio.wait_for(
            strategy_fn(query, **kwargs),
            timeout=timeout_ms / 1000,
        )
        return result
    except (asyncio.TimeoutError, Exception):
        return None
```

---

## 13. Evaluation of query understanding

Query understanding is unusually difficult to evaluate because its output (manufactured queries)
is not the final output — it is an intermediate that influences retrieval, which influences
generation. The question is not "is the rewritten query good?" but "did the rewritten query
improve retrieval, and did the improved retrieval improve the answer?"

### 13.1 The ablation methodology

The gold-standard evaluation is ablation: measure the system with and without each query
understanding strategy, on the same query set, and compare end-to-end metrics.

```
    Configuration                   recall@20  nDCG@10   answer_quality   p50 (ms)
    ─────────────────────────────   ─────────  ────────  ──────────────   ────────
    1. Original query only           0.72       0.65      3.2              120
    2. + rewriting                   0.78       0.71      3.5              420
    3. + HyDE                        0.76       0.68      3.4              620
    4. + decomposition               0.74       0.66      3.3              520
    5. + rewrite + HyDE              0.81       0.74      3.7              650
    6. + rewrite + decompose         0.80       0.72      3.6              530
    7. + all three                   0.82       0.74      3.7              720
```

In this (illustrative) table:

- Rewriting provides the largest single-strategy gain (+6pp recall, +0.06 nDCG)
- HyDE provides a smaller but meaningful gain (+4pp recall)
- Decomposition helps minimally on this query set (most queries were single-hop)
- Rewrite + HyDE provides the best gain, nearly as good as all three
- All three adds 1pp recall over rewrite + HyDE but adds 70ms latency

The decision: **rewrite + HyDE is the operating point** for this corpus and query distribution.
Decomposition is not worth its cost on this query set, though it may be worth routing to for the
subset of complex queries.

### 13.2 Per-stratum evaluation

Aggregate metrics hide the most important signal: *which query types benefit from which
strategies?* The evaluation must be stratified by query classification:

```python
from collections import defaultdict


def evaluate_per_stratum(
    eval_set: list[dict], pipeline: QueryUnderstandingPipeline,
    retriever: Any, metrics_fn: Any,
) -> dict[str, dict[str, float]]:
    """Evaluate query understanding broken down by complexity x intent stratum."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for ex in eval_set:
        cls = classify_query_heuristic(ex["query"])
        stratum = f"{cls.complexity.value}_{cls.intent}"
        qu = pipeline.process(ex["query"])          # await in practice
        results = [retriever.search(q, top_k=20) for q in qu.all_retrieval_queries]
        fused = reciprocal_rank_fusion(results)
        buckets[stratum].append(metrics_fn(fused, ex["relevant_chunk_ids"]))
    return {
        s: {"recall@20": sum(m["recall@20"] for m in ms) / len(ms),
            "nDCG@10":   sum(m["nDCG@10"] for m in ms) / len(ms),
            "count": len(ms)}
        for s, ms in buckets.items()
    }
```

The per-stratum table is the artifact that drives routing decisions. If HyDE helps troubleshooting
queries by +12pp recall but hurts lookup queries by -3pp, the routing table should apply HyDE to
troubleshooting queries only.

### 13.3 Measuring whether the rewrite preserved intent

A common failure mode (§14.1) is the rewrite changing the query's intent. Measuring this requires
a separate evaluation: for each query-rewrite pair, does the rewrite still mean the same thing?

This can be automated with an LLM judge:

```python
INTENT_PRESERVATION_PROMPT = """\
Does the rewritten query preserve the EXACT intent of the original query?

Original: {original}
Rewritten: {rewritten}

Answer YES if the rewritten query would retrieve documents that answer the same question.
Answer NO if the rewritten query changes what information is being sought.

Respond with only YES or NO, then a one-sentence explanation.
"""


async def check_intent_preservation(
    original: str,
    rewritten: str,
    llm_client: Any,
) -> tuple[bool, str]:
    """Check whether a rewrite preserved the original query's intent.
    
    Returns (preserved: bool, explanation: str).
    """
    response = await llm_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": INTENT_PRESERVATION_PROMPT.format(
                original=original, rewritten=rewritten,
            ),
        }],
    )
    
    text = response.content[0].text.strip()
    preserved = text.upper().startswith("YES")
    return preserved, text
```

### 13.4 A/B testing query understanding

Ablation on a static eval set tells you how much each strategy helps on known queries. A/B
testing tells you how much it helps on *real* traffic. The two are complementary:

- **Ablation** is for development: fast iteration, controlled comparisons, reproducible
- **A/B testing** is for production: real query distribution, real latency, real cost

The A/B test design for query understanding:

```
    Control (50% of traffic):   original query only → retrieval → generation
    Treatment (50% of traffic): query understanding pipeline → retrieval → generation
```

Metrics to track:

| Metric | What it measures | How to compute |
|---|---|---|
| Retrieval recall | did QU improve retrieval? | requires relevance judgments (expensive) |
| Answer quality (LLM judge) | did QU improve answers? | automated judge on a sample |
| User satisfaction (thumbs up/down) | did users notice? | implicit feedback |
| Latency (p50, p99) | what did QU cost? | instrumentation |
| Cost per query | what did QU cost in dollars? | billing |
| Cache hit rate | is the cache working? | instrumentation |

The most informative metric is answer quality, but it is also the most expensive to measure.
In practice, teams run A/B tests with user satisfaction as the primary metric and answer quality
on a judged sample as the secondary metric.

### 13.5 The baseline trap

A subtle evaluation error: comparing query understanding against a *bad* baseline. If the
baseline retrieval system is poorly configured (wrong embedding model, bad chunking, no hybrid
search), query understanding will show large gains — because it is compensating for upstream
failures that should have been fixed upstream.

The honest evaluation baseline is a well-configured retrieval system *without* query understanding.
That means:

- The right embedding model for the domain (`01`)
- Good chunking (`02`)
- Properly configured hybrid search (`04`)
- A reranker

If query understanding does not improve this baseline, the answer is "you don't need it yet" —
not "try a different rewriting prompt."

---

## 14. Failure modes and debugging

Query understanding can fail in ways that are invisible without instrumentation. The manufactured
queries are intermediate artifacts that are not shown to the user, and their effect on retrieval
is indirect. When the system produces a bad answer, the failure could be in query understanding,
retrieval, or generation — and without traces (`10`), you cannot tell which.

### 14.1 The rewrite that changes intent

The most common and most damaging failure: the LLM rewrite changes the meaning of the query.

```
    Original: "How do I delete a user WITHOUT deleting their data?"
    Rewrite:  "How to delete user data"
```

The rewrite dropped the negation. This is not a rare failure — negation, qualification, and
scope restrictions are exactly the parts of queries that LLMs handle least reliably when
compressing.

**Detection:** log every (original, rewrite) pair and run the intent-preservation check (§13.3)
on a sample. Any rewrite that changes intent is a bug in the rewriting prompt or the model's
instruction following.

**Mitigation:** (1) The original query always runs alongside the rewrite — but the rewritten
query's results may outrank the original query's results in fusion, pushing the original answer
down. (2) Add explicit rules to the rewriting prompt about preserving negation and scope. (3)
Use a more capable model for the rewrite step — cost goes up, but so does instruction following.

### 14.2 Over-specified rewrites

The rewrite adds specificity that the user did not intend:

```
    Original: "how does caching work"
    Rewrite:  "how does Redis caching with TTL-based expiration work in microservices"
```

The user might have been asking about browser caching, or DNS caching, or the pipeline's own
semantic cache. The rewrite has locked in one interpretation. This is a form of premature
disambiguation — and it is particularly dangerous because it *looks* like a good rewrite (it is
more specific!) while actually narrowing the retrieval in a way the user did not ask for.

**Detection:** track the length ratio of rewrites to originals. Rewrites that are more than 2x
longer than the original are suspect. Also measure retrieval precision — over-specified rewrites
increase precision (fewer results, more focused) but decrease recall (the right answer may be
in a different interpretation).

**Mitigation:** the routing table should flag ambiguous queries for multi-query (§6) rather than
rewriting. Multi-query generates multiple interpretations; rewriting picks one.

### 14.3 Hallucinated domain terms in rewrites

The LLM rewrite introduces technical terms that do not exist in the corpus:

```
    Original: "how to set up monitoring"
    Rewrite:  "configure Prometheus Grafana observability stack monitoring setup"
```

If the corpus documents a custom monitoring solution that is not Prometheus/Grafana, the rewrite
has introduced terms that will retrieve irrelevant chunks from other parts of the corpus (if
they exist) or nothing at all.

**Detection:** cross-reference the rewrite's terms against the corpus vocabulary. Terms in the
rewrite that appear fewer than N times in the corpus are likely hallucinated.

**Mitigation:** provide domain context in the rewriting prompt — a list of the corpus's main
topics, or a few example chunk titles. The few-shot examples in §3.3 serve this function.

### 14.4 Decomposition that loses the relationship

A query asks about the *relationship* between two things. Decomposition splits it into sub-queries
about each thing independently, losing the relationship:

```
    Original: "How does the retry policy interact with the circuit breaker?"
    Sub-query 1: "What is the retry policy?"
    Sub-query 2: "How does the circuit breaker work?"
```

Neither sub-query retrieves chunks about the *interaction*. The retrieval system finds general
information about retries and general information about circuit breakers, but nothing about
their interaction — which is what the user asked.

**Detection:** intent-preservation check on the sub-queries: does answering all sub-queries
provide the information needed to answer the original query?

**Mitigation:** the decomposition prompt should include a rule about preserving relationships,
and the LLM should be prompted to keep at least one sub-query about the interaction itself:
"How does the retry policy interact with the circuit breaker?" alongside the component queries.

### 14.5 HyDE generating the wrong hypothetical

The LLM generates a hypothetical answer that is factually wrong in a way that pulls the embedding
toward the wrong neighborhood:

```
    Query: "What's the maximum file upload size?"
    HyDE: "The maximum file upload size is 100MB. Files larger than 100MB
           must be split into chunks and uploaded via the multipart API."
    Actual: "The maximum file upload size is 2GB. The system supports direct
            upload for files up to 2GB without chunking."
```

The HyDE embedding is in the neighborhood of "100MB limit, multipart upload" — which is a
different topic than "2GB limit, direct upload." If there are chunks about multipart uploads in
the corpus, they will be retrieved instead of the correct limit chunk.

**Detection:** compare HyDE retrieval results against original-query retrieval results. When they
diverge significantly (Jaccard similarity of top-10 < 0.3), the HyDE answer is likely pulling
retrieval in the wrong direction.

**Mitigation:** always run the original query alongside HyDE, and use RRF to fuse — the correct
chunk should appear in the original query's results even if HyDE's results are wrong. Also
consider routing: HyDE is less harmful for "how" and "why" queries (where the hypothetical's
structure matters more than its specific facts) than for "what" queries (where the specific
facts matter).

### 14.6 The multi-query redundancy trap

Multi-query generation produces variants that are too similar to each other, providing no
additional coverage:

```
    Original: "how to configure authentication"
    Variant 1: "configuring authentication settings"
    Variant 2: "authentication configuration guide"
    Variant 3: "set up authentication configuration"
```

All four queries will retrieve nearly identical result sets. The cost of four retrievals is paid
with no recall gain.

**Detection:** measure the Jaccard similarity of the result sets. If top-10 Jaccard > 0.8 across
all pairs, the variants are redundant.

**Mitigation:** the multi-query prompt should emphasize *different vocabulary* rather than
*different phrasing*. Temperature > 0 helps. Generating 3 diverse variants is better than 5
similar ones. Also: multi-query is only worth its cost when there is genuine vocabulary
ambiguity; for most queries, a single rewrite is sufficient.

### 14.7 Debugging workflow

When a query produces a bad answer, the debugging workflow for query understanding is:

```
    1. Inspect the trace (10):
       - What was the raw query?
       - Was coreference resolution triggered? What did it produce?
       - What classification was assigned?
       - What strategies were applied?
       - What were the manufactured queries?
       
    2. For each manufactured query:
       - What did it retrieve? (top-10 with scores)
       - Did any manufactured query retrieve the correct chunk?
       - Did any manufactured query retrieve distracting chunks?
    
    3. After fusion:
       - Is the correct chunk in the fused set?
       - If yes: the problem is downstream (reranking, context, generation)
       - If no: the problem is upstream (query understanding or indexing)
    
    4. If the correct chunk is not in the fused set:
       - Does any plausible query retrieve it? (manual search)
       - If no: the problem is indexing (02, 03) or the chunk doesn't exist
       - If yes: the manufactured queries were not good enough
         → inspect the rewriting/decomposition prompts
         → check the classification and routing decision
```

This workflow requires that every step in the pipeline is logged with its inputs and outputs.
`10` is how you make this structural rather than ad-hoc.

---

## 15. Anti-patterns

**Anti-pattern 1: Running every strategy on every query.**
Teams discover query rewriting, HyDE, multi-query, and decomposition, and chain them all into a
fixed pipeline that runs on every query. A simple lookup like "What is the API rate limit?" goes
through rewriting (unnecessary — it is already precise), HyDE (actively harmful — the hypothetical
answer fabricates a wrong limit), decomposition (pointless — it is a single-hop question), and
multi-query (redundant — the variants retrieve the same chunks). The result is 3-4 LLM calls,
1-2 seconds of added latency, and retrieval quality that is equal to or worse than the original
query alone.
**Why it fails:** query understanding strategies are not universally beneficial. Each addresses a
specific mismatch type (§1.1), and applying a strategy to a query without that mismatch adds cost
with no gain (§10) and risk of degradation (§14). The fix is routing (§2): classify the query,
apply only the strategies that address the identified mismatch.

**Anti-pattern 2: Replacing the original query with the rewrite.**
The manufactured query replaces the original query instead of running alongside it. When the
rewrite is wrong (§14.1, §14.2), the original query's results are lost entirely, and the system
fails on a query it would have handled correctly without any query understanding.
**Why it fails:** this violates the cascade property (`04` §1). The original query is a branch.
The rewrite is another branch. Fusion (RRF) combines them. The original query should always run
as a branch; the rewrite is additive.

**Anti-pattern 3: Caching without invalidation.**
Query transformation caches are deployed with no TTL and no invalidation hook. The corpus is
updated weekly, but the cached rewrites reflect the old corpus's vocabulary. A rewrite that was
optimal for the old corpus retrieves the wrong chunks from the new corpus.
**Why it fails:** a rewrite is optimal for a specific corpus state. When the corpus changes, the
optimal rewrite changes. Caches must have TTLs tied to the corpus update frequency, or they
must be invalidated on reindex.

**Anti-pattern 4: HyDE on factual lookups.**
HyDE is applied to queries like "What is the default value of max_connections?" The hypothetical
answer fabricates a specific number. The embedding of the hypothetical is near the wrong chunk,
and the correct chunk (with the actual number) is not the nearest neighbor.
**Why it fails:** HyDE works by generating text in the *style* of the answer. For factual
lookups, the style includes the specific fact, and a wrong fact pulls the embedding to the wrong
neighborhood. HyDE should be routed to conceptual and troubleshooting queries, not factual
lookups.

**Anti-pattern 5: Decomposing every multi-word query.**
Any query longer than 10 words triggers decomposition. "How do I configure the retry policy for
HTTP requests in the API gateway?" is decomposed into "What is a retry policy?", "What are HTTP
requests?", "What is an API gateway?" — three sub-queries that are each less useful than the
original.
**Why it fails:** query length is not complexity. A long, specific, single-topic query does not
need decomposition — it needs at most a rewrite. Decomposition is for queries with *multiple
distinct retrieval needs*, not for long queries. The classification step (§2) should gate
decomposition on multi-hop structure, not length.

**Anti-pattern 6: Not logging the manufactured queries.**
The rewritten and decomposed queries are not included in the trace. When a retrieval failure
occurs, the debugging workflow (§14.7) cannot proceed because the manufactured queries are not
visible.
**Why it fails:** you cannot debug what you cannot see. Every manufactured query, its retrieval
results, and the fusion step must be logged. `10` exists to make this systematic. Without it,
query understanding is a black box inside a black box.

**Anti-pattern 7: Using the most expensive model for query understanding.**
Teams use their most capable (and most expensive) model for rewriting, classification, and HyDE —
the same model they use for generation. The query understanding layer costs as much as the
generation layer, doubling the total cost.
**Why it fails:** query understanding tasks (rewriting a query, classifying intent, generating a
short hypothetical answer) are significantly simpler than generation (synthesizing a multi-chunk
answer). A smaller, faster, cheaper model handles them well. Using a larger model buys marginal
quality improvement at disproportionate cost and latency. Route the query understanding LLM calls
to a fast, cheap model; reserve the expensive model for generation.

**Anti-pattern 8: Conversational resolution without a standalone-query check.**
Every query in a conversation goes through coreference resolution, including queries that are
already standalone. "What is Kubernetes?" in a conversation about Kubernetes is already complete;
running it through coreference resolution risks introducing context that the user did not intend
("What is Kubernetes in the context of our deployment pipeline?").
**Why it fails:** the heuristic check (§2.2, §9.1) is cheap and can detect standalone queries.
Only queries with pronouns, ellipsis, or conversational markers need resolution. Running
resolution unconditionally adds an LLM call to every conversational query.

**Anti-pattern 9: Multi-query without deduplication of results.**
Multiple query variants retrieve overlapping result sets, and the pipeline processes all duplicates
through the reranker. If each of 4 variants retrieves 20 results and 60% overlap, the reranker
processes 80 results instead of 32 unique ones — 2.5x the cost for the same information.
**Why it fails:** RRF deduplicates by design (it tracks unique chunk IDs), but if you run
reranking *before* fusion rather than after, you rerank duplicate chunks redundantly. The correct
order is: retrieve per query, fuse with RRF (which deduplicates), then rerank the fused set once.

**Anti-pattern 10: Evaluating query understanding by rewrite quality instead of retrieval
quality.**
Teams judge rewrites by reading them and deciding whether they "look good." A rewrite can look
good (grammatically correct, specific, on-topic) and still hurt retrieval — because the rewrite's
vocabulary does not match the corpus's vocabulary, or because it over-specifies, or because the
original query was already optimal.
**Why it fails:** the quality of a rewrite is determined by its effect on retrieval, not by its
surface form. A rewrite that looks terrible ("kubernetes pod evict ephemeral storage OOM") but
retrieves the right chunk is a better rewrite than one that looks polished ("What are the common
causes of Kubernetes pod eviction?") but retrieves generic chunks. Evaluation must be end-to-end
(§13).

---

## 16. Mental models — the compressed set

- **A query is raw material, not a retrieval input.** Every technique here manufactures better
  retrieval inputs from the same raw query. The manufacturing cost (LLM calls) must be justified
  by the retrieval gain, per query type, per corpus.

- **The original query always runs.** Manufacturing is additive. Each manufactured query is an
  additional branch in the cascade. Bad branches add noise that the reranker discards; they do
  not remove signal.

- **Routing is the most important decision.** Which strategy to apply to which query determines
  more of the outcome than any individual strategy's implementation quality. A well-routed system
  with simple strategies outperforms a poorly-routed system with sophisticated strategies.

- **The cost is serial.** Query understanding adds latency before retrieval, not during it.
  Strategies can be parallelized, but the slowest strategy determines the added latency. The
  cost model (§10) is the constraint that makes routing decisions concrete.

- **Every technique addresses a specific mismatch type.** Rewriting fixes vocabulary mismatch.
  Step-back fixes abstraction-level mismatch. Decomposition fixes complexity. HyDE fixes
  question-answer perspective mismatch. Multi-query covers vocabulary uncertainty. Expansion
  adds terms for lexical search. Applying the wrong technique to the wrong mismatch wastes
  cost and may hurt.

- **HyDE is powerful and dangerous.** It works when the LLM's prior over the answer space is
  reasonable and the query-answer gap is large. It hurts when the prior is wrong (novel domains)
  or when the query is already precise (factual lookups). Route carefully.

- **Coreference resolution is not optional in conversational settings.** An unresolved pronoun is
  a guaranteed retrieval failure. Resolution is the one strategy that is almost never wrong to
  apply, and it must come first in the pipeline.

- **The cache amortizes the cost.** Exact-match or semantic caching of manufactured queries
  eliminates the LLM call cost for repeated queries. The cache must have a TTL tied to corpus
  update frequency.

- **Evaluation must be end-to-end.** A good-looking rewrite that does not improve retrieval is
  not a good rewrite. Evaluate query understanding by its effect on recall, nDCG, and answer
  quality — not by reading the rewrites. Ablation (§13.1) and per-stratum analysis (§13.2) are
  the tools.

- **Log everything.** Every manufactured query, every classification, every routing decision.
  If you cannot see the rewritten query in your traces, you cannot debug the retrieval failure
  it caused (§14.7).

---

## 17. Lab exercises

**Lab 1 — The baseline: how much does your retrieval actually need query understanding?**
*Goal:* establish whether raw queries fail on your corpus, and on which query types, before
investing in any manufacturing strategy.
*Steps:* take your eval set from `08`. Run every query through your retrieval pipeline as-is
(no rewriting, no decomposition). Record recall@20 and nDCG@10 per query. Classify each query
using the heuristic classifier (§2.2). Group by classification stratum. For each stratum, compute
the mean recall and the count of queries with recall@20 = 0 (total misses). The strata with low
recall are where query understanding can help; strata with high recall already are where it would
waste cost.
*Artifact:* a stratum-level table of recall@20, nDCG@10, and zero-recall count, plus a written
conclusion identifying which strata need manufacturing and which do not.
*Success criterion:* you can name which query types fail retrieval and which do not, with numbers.
Be willing to accept "retrieval is already good enough; no query understanding needed" as a valid
and money-saving outcome.
*Time:* ~2 hours.
*Unblocks:* every other lab here.

**Lab 2 — Query rewriting, measured rather than assumed.**
*Goal:* measure the effect of query rewriting on each stratum, including strata where it hurts.
*Steps:* implement the rewriting pipeline (§3.2). Run every eval query through it. For each
query, retrieve with the original query and with the rewritten query separately, then fuse with
RRF. Measure recall@20 and nDCG@10 for: (a) original only, (b) rewrite only, (c) original +
rewrite fused. Break down by stratum. Identify the strata where the rewrite helps and the strata
where it hurts or is neutral. Run the intent-preservation check (§13.3) on 50 random rewrites.
*Artifact:* a three-column table by stratum (original, rewrite, fused), plus the
intent-preservation rate.
*Success criterion:* you can state which strata benefit from rewriting, by how much, and what
fraction of rewrites change intent. If the intent-preservation rate is below 95%, your rewriting
prompt needs work before rewriting is production-safe.
*Time:* ~3 hours.
*Unblocks:* lab 5 (the routing table), lab 6 (the pipeline), and `06`.

**Lab 3 — HyDE on your corpus.**
*Goal:* measure HyDE's effect per stratum, especially the factual-lookup stratum where it is
expected to hurt (§7.4).
*Steps:* implement HyDE (§7.2). Run every eval query through it. For each query, retrieve with
the original embedding and with the HyDE embedding separately, then fuse. Measure recall@20 for:
(a) original only, (b) HyDE only, (c) original + HyDE fused. Break down by stratum. Pay
particular attention to the troubleshooting and explanation strata (where HyDE should help) and
the lookup stratum (where it should hurt or be neutral). Generate 1, 2, and 3 hypothetical
documents and compare to find the knee.
*Artifact:* a stratum-level table for original vs. HyDE vs. fused, plus a hypothetical-count
curve showing the marginal gain of each additional hypothetical document.
*Success criterion:* you know on which strata HyDE helps and by how much, and you have a measured
answer to "how many hypothetical documents?"
*Time:* ~4 hours.
*Unblocks:* lab 5, lab 6.

**Lab 4 — Multi-query redundancy measurement.**
*Goal:* measure the diversity of multi-query variants and the marginal recall gain of each
additional variant.
*Steps:* implement multi-query generation (§6.1). For each eval query, generate 1 through 5
variants. At each count, fuse and measure recall@20. Also compute pairwise Jaccard similarity
of the top-10 result sets across variants. Plot: (x) variant count, (y1) recall@20, (y2) mean
pairwise Jaccard. Find the knee where additional variants stop adding recall, and the point where
Jaccard exceeds 0.8 (redundancy threshold).
*Artifact:* a two-panel chart (recall vs. variants, Jaccard vs. variants), with the knee annotated.
*Success criterion:* a measured variant count (typically 2-3) and evidence for whether multi-query
is worth its cost on your corpus versus a single rewrite.
*Time:* ~3 hours.
*Unblocks:* lab 5, lab 6.

**Lab 5 — The routing table, populated with measurements.**
*Goal:* build a routing table that maps query classification to manufacturing strategy, backed by
the per-stratum measurements from labs 1-4.
*Steps:* for each classification stratum, select the strategy (or combination) that maximizes
recall within the latency budget from labs 1-4. Encode the routing table in the format of §2.4.
Then run the full eval set through the routing table and compare the routed pipeline's recall and
nDCG against: (a) no query understanding, (b) rewrite-everything, (c) HyDE-everything. The
routed pipeline should match or exceed the best single-strategy pipeline at lower cost.
*Artifact:* the routing table, plus a four-column comparison (baseline, rewrite-all, HyDE-all,
routed) with recall, nDCG, mean latency, and cost per query.
*Success criterion:* the routed pipeline's quality matches or exceeds the best single strategy,
at lower mean latency and cost.
*Time:* ~3 hours.
*Unblocks:* lab 6.

**Lab 6 — The full pipeline, end-to-end.**
*Goal:* integrate routing, manufacturing, caching, and retrieval into the pipeline from §12 and
measure end-to-end performance.
*Steps:* implement the `QueryUnderstandingPipeline` from §12.2. Run the full eval set through
it. Measure: recall@20, nDCG@10, answer quality (if you have a judge from `08`), p50, p99,
mean cost per query, and cache hit rate (after a warmup pass). Break down by stratum. Verify
that the pipeline's fallback behavior works: disable the LLM and confirm that every query still
returns results (using the original query).
*Artifact:* the end-to-end table with all metrics, the cache hit rate, and a fallback test result.
*Success criterion:* the pipeline improves recall by at least 3pp over no-QU baseline on the
worst-performing stratum, without regressing on any stratum, within the latency budget.
*Time:* ~half a day.
*Unblocks:* `06`, `10`, production deployment.

**Lab 7 — Conversational resolution accuracy.**
*Goal:* measure coreference resolution accuracy on multi-turn conversations.
*Steps:* construct (or extract from logs) 50 multi-turn conversation fragments where the last
turn requires coreference resolution. For each, manually write the "gold" standalone query.
Run the resolution pipeline (§9.1) on each. Measure: (a) exact match rate (unlikely to be high),
(b) intent-preservation rate (§13.3), (c) retrieval recall with resolved vs. unresolved query.
*Artifact:* a resolution accuracy table plus a recall comparison (unresolved vs. resolved).
*Success criterion:* the resolved query retrieves the correct chunk at least 90% of the time when
the unresolved query fails (pronoun-heavy queries), and the intent-preservation rate is above 95%.
*Time:* ~4 hours.
*Unblocks:* lab 6 (for conversational deployments).

**Lab 8 — Decomposition on complex queries.**
*Goal:* measure decomposition's effect specifically on the complex-query stratum.
*Steps:* select 30 complex queries from your eval set (or construct them). For each, run the
decomposition pipeline (§5.2) and inspect the sub-queries. Retrieve with sub-queries separately,
then fuse. Compare recall@20 for: (a) original query only, (b) decomposed sub-queries fused,
(c) original + sub-queries fused. Check the intent-preservation of each sub-query set: does
answering all sub-queries provide the information needed to answer the original?
*Artifact:* the three-configuration recall table, plus a manual inspection of 10 decompositions
noting any intent loss.
*Success criterion:* decomposition improves recall on complex queries by at least 5pp, and
the sub-queries collectively cover the original query's information needs in at least 80% of
cases.
*Time:* ~3 hours.
*Unblocks:* lab 5, lab 6.

**Lab 9 — Cost-latency Pareto frontier.**
*Goal:* map the quality-cost-latency tradeoff for your query understanding configuration.
*Steps:* define 5-7 configurations spanning the complexity range: (1) no QU, (2) heuristic-only
routing with rewrite, (3) LLM classification with rewrite, (4) rewrite + HyDE routed, (5) full
pipeline routed, (6) full pipeline on every query (the anti-pattern), (7) full pipeline with
caching. For each, measure recall@20, nDCG@10, p50, p99, and cost per 1000 queries on the eval
set.  Plot the Pareto frontier: (x) cost per 1000 queries, (y) recall@20, with each point
labeled by its configuration and its p99 annotated.
*Artifact:* the Pareto plot plus the configuration table.
*Success criterion:* you can point to the configuration on the Pareto frontier that meets your
quality and latency requirements, and you have a number for how much quality you would gain (and
what it would cost) to move to the next configuration up.
*Time:* ~half a day.
*Unblocks:* production deployment, `11-token-accounting-and-cost.md`, `12`.

**Lab 10 — Query understanding tracing integration.**
*Goal:* ensure that every query understanding step is visible in the trace.
*Steps:* instrument the pipeline from lab 6 with OpenTelemetry spans (or your tracing system from
`10`). Each span should capture: the strategy name, the input (raw or resolved query), the
output (manufactured query or classification), the latency, and the model used. Run 10 queries
and verify in the trace UI that: (a) every strategy is a distinct span, (b) the manufactured
queries are visible, (c) the retrieval spans show which manufactured query produced which results,
(d) the cache hit/miss is recorded.
*Artifact:* a screenshot of a trace showing the full query understanding pipeline, plus a list
of the span attributes recorded.
*Success criterion:* the debugging workflow from §14.7 can be executed entirely from the trace,
without reading code or logs.
*Time:* ~3 hours.
*Unblocks:* `10`, production debugging.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — why HyDE bridges the
question-answer embedding gap, why decomposition helps multi-hop queries, why rewriting can
change intent, why the cascade property makes manufactured queries fail-safe as additional
branches, why routing dominates any single technique's quality — are derivable from the
definitions and from the retrieval model established in `04`. The latency figures in §10.1 are
explicitly labeled as ballpark estimates for hosted APIs and should not be quoted as measurements;
§17's labs 6 and 9 are how you get yours.

**Structural claims verified against cited chapters:** the cascade property and its recall-ceiling
consequence (`04` §1), the fusion step as the entry point for additional branches (`04` §5), the
asymmetric embedding distinction (`01` §3), the canonical chunk text as a shared target (`02`
§4.3), and the recall ceiling as the governing constraint (`00` §4). The vocabulary-mismatch
taxonomy in §1.1 is a classification of known retrieval failures, organized by the manufacturing
strategy that addresses each; the individual failure types are documented in `04` §3.1's failure
table for dense retrieval and `04` §2.3's strength table for BM25, unified here from the query
perspective.

**Deliberately not in this document:** any specific rewriting-quality leaderboard, any claim that
one rewriting model outperforms another on a public benchmark, and any assertion that a specific
technique always helps. The effectiveness of every technique here depends on the corpus, the
embedding model, the query distribution, and the retrieval pipeline configuration — and §17's labs
are how you determine what works for your system. The code examples are production-quality
patterns, not tuned implementations; the prompt templates should be adapted to your corpus's
vocabulary and domain.

The labs in §17 are what convert this to **rung 1 — measured**, and their outputs must always
travel with their configuration — including the model used for query understanding calls, the
embedding model, the corpus version, and the routing table.
