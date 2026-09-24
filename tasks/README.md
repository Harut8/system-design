# System Design Tasks

Twenty-one interview-style design problems. Each has a full reference solution in
[`../solutions/`](../solutions/) and ends with an **Interview Kit**: what to read first,
curveballs an interviewer would throw, must-answer security and privacy questions, and a
phased delivery plan.

## Index

| Task | Level | Time box | Core topics | Solution |
|---|---|---|---|---|
| [Distributed counter](distributed-counter.md) | Mid | 45 min | Hot keys, idempotency, write aggregation, caching | [solution](../solutions/distributed-counter-design.md) |
| [Instagram feed](instagram-feed.md) | Mid | 45 min | Fan-out on write vs. read, caching, ranking | [solution](../solutions/instagram-feed-design.md) |
| [Twitter search](twitter-search.md) | Mid–Senior | 45 min | Inverted index, real-time ingestion, visibility filtering | [solution](../solutions/twitter-search-design.md) |
| [API gateway + rate limiter](api-gateway-rate-limiter.md) | Mid–Senior | 45 min | Token bucket / GCRA, distributed limits, config safety | [solution](../solutions/api-gateway-rate-limiter-design.md) |
| [FastAPI RBAC](fastapi-rbac.md) | Mid | 45 min, plus code | Roles, tenancy, permission caching, audit | [solution](../solutions/fastapi-rbac-design.md) |
| [Job scheduler on Postgres](job-scheduler.md) | Senior | 60 min | `SKIP LOCKED`, leases, vacuum, fairness | [solution](../solutions/job-scheduler-postgres-deep-dive.md) |
| [Key-value store](key-value-store.md) | Senior–Staff | 60 min | Raft, partitioning, linearizability, leases | [solution](../solutions/key-value-store-design.md) |
| [IBKR-style trading platform](ibkr-trading-platform.md) | Staff | 60 min | OMS, risk, market data, compliance, idempotency | [solution](../solutions/ibkr-trading-platform-design.md) |
| [Feature store](feature-store.md) | Senior | 60 min | Point-in-time joins, streaming features, skew | [solution](../solutions/feature-store-design.md) |
| [Recommendation system](recommendation-system.md) | Senior–Staff | 60 min | Two-tower retrieval, ANN, multi-stage ranking | [solution](../solutions/recommendation-system-design.md) |
| [ML inference platform](ml-inference-platform.md) | Senior–Staff | 60 min | Batching, autoscaling GPUs, LLM serving, registry | [solution](../solutions/ml-inference-platform-design.md) |
| [Parallel ML training](parallel-ml-training.md) | Staff | 60 min | 3D parallelism, gang scheduling, checkpointing | [solution](../solutions/parallel-ml-training-design.md) |
| [DAG pipeline orchestration](dag-pipeline-orchestration.md) | Senior | 60 min | Scheduler loop, executors, backfills, metadata DB | [solution](../solutions/dag-pipeline-orchestration-design.md) |
| [Workflow orchestration](workflow-orchestration.md) | Staff | 60 min | Event sourcing, deterministic replay, sharding | [solution](../solutions/workflow-orchestration-design.md) |
| [RAG platform](rag-platform.md) | Senior–Staff | 60 min | Hybrid retrieval, ACLs, evaluation, injection | [solution](../solutions/rag-platform-design.md) |
| [AI search engine](ai-search-engine.md) | Staff | 60 min | Crawling, index tiers, ranking, answer generation | [solution](../solutions/ai-search-engine-design.md) |
| [LLM gateway](llm-gateway.md) | Senior | 45 min | Provider routing, fallbacks, token budgets | [solution](../solutions/llm-gateway-design.md) |
| [Agent orchestration](agent-orchestration.md) | Staff | 60 min | Planning, durable runs, human oversight | [solution](../solutions/agent-orchestration-design.md) |
| [AI agent platform](ai-agent-platform.md) | Staff | 60 min | Multi-team platform, credentials, evals | [solution](../solutions/ai-agent-platform-design.md) |
| [Tool platform](tool-platform.md) | Staff | 60 min | Tool registry, sandboxes, on-behalf-of auth | [solution](../solutions/tool-platform-design.md) |
| [AI observability & evaluation](ai-observability-evaluation.md) | Senior–Staff | 60 min | Tracing, LLM judges, sampling, privacy | [solution](../solutions/ai-observability-evaluation-design.md) |

**Suggested order:** counter → feed → rate limiter → job scheduler → key-value store for the
distributed-systems core. Then feature store → recommendation → inference for ML. Then LLM
gateway → RAG → tool platform → agent orchestration for AI systems.

## How to practice

1. **Time-box it** (45 or 60 minutes, see the index) and answer out loud or on paper before
   opening the solution.
2. Follow this shape. Interviewers score structure as much as content:

| Minutes (of 45) | Step | Output |
|---|---|---|
| 0–5 | Clarify | Functional scope, non-goals, the two or three numbers that drive the design |
| 5–10 | Estimate | QPS (peak, not average), storage growth, the read/write ratio, one cost figure |
| 10–20 | High-level design | Boxes and arrows, the data model, the API for the main call |
| 20–35 | Deep dives | The two hardest parts, chosen by you, with trade-offs stated |
| 35–40 | Failure and security | What breaks first, what happens then, who can see which data |
| 40–45 | Evolution | MVP → Growth → Scale, and what you would *not* build yet |

3. Then work through the task's **Interview Kit** curveballs without looking at the hints.
4. Read the solution and score yourself with the rubric below. Redo the task a week later.

## Scoring rubric

Score each row 1–4. Around 20 of 28 is a pass at senior level. Staff level needs no row
below 3.

| Dimension | 1: Weak | 2: Developing | 3: Strong | 4: Exceptional |
|---|---|---|---|---|
| **Requirements** | Starts drawing immediately | Lists features, no numbers | Scope, non-goals and the key numbers stated up front | Finds the requirement that changes the design (e.g. "exact counts only below 1K") |
| **Estimation** | None | Numbers without using them | Peak QPS, storage and cost estimated *and used* to pick components | Estimates expose the bottleneck before the design does |
| **Architecture** | Box soup, no data flow | Reasonable boxes, vague data model | Clear data model, API, read and write paths | Simplest design that meets the numbers, with a named reason for each component |
| **Deep dives** | Stays at the surface | Explains one component | Two hard parts in depth, with alternatives compared | Quantifies the trade-off (latency, cost, consistency) and names when the choice flips |
| **Failure handling** | "We'll add retries" | Lists failures | Behaviour under node, zone and dependency failure. Retries have budgets, overload is shed | Recovery is designed too: no thundering herds, backlog handled, blast radius bounded (cells, shuffle sharding) |
| **Security & privacy** | Not mentioned | "Use HTTPS and auth" | Authn/z at every boundary, tenant isolation, secrets, PII handling and deletion | Threat model specific to *this* system (IDOR, injection, abuse), with GDPR/PCI trade-offs resolved |
| **Evolution & operations** | Only the end state | Mentions monitoring | MVP → Scale phases, SLOs, key alerts, cost awareness | Says what not to build yet and what signal would trigger the next phase |

## Reference material

The chapters these tasks draw on: [`../distributed-systems/`](../distributed-systems/),
[`../databases/`](../databases/), [`../ai-rag/`](../ai-rag/) and
[`../sre-observability/`](../sre-observability/). Pattern references with no matching task:
[API design patterns](../solutions/api-design-patterns.md),
[API message patterns](../solutions/api-message-patterns.md),
[Big-tech API standards](../solutions/big-tech-api-standards.md),
[Database design best practices](../solutions/database-design-best-practices.md),
[Write-ahead log deep dive](../solutions/write-ahead-log-deep-dive.md) and
[Zero-GC Python](../solutions/high-performance-python-zero-gc.md).
