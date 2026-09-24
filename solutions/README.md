# Solutions

Reference designs for every task in [`../tasks/`](../tasks/README.md), plus pattern references.
Each design covers requirements, capacity estimates, architecture, deep dives, failure
walkthroughs, trade-offs and an evolution path. The ones marked 🔒 have a dedicated
**Security, Privacy, and Abuse Prevention** section. The rest handle security within their
main sections (auth, tenancy, guardrails).

## Designs

| Area | Solution | Task |
|---|---|---|
| Distributed systems | [Distributed counter](distributed-counter-design.md) 🔒 | [task](../tasks/distributed-counter.md) |
| | [Instagram feed](instagram-feed-design.md) | [task](../tasks/instagram-feed.md) |
| | [Twitter search](twitter-search-design.md) 🔒 | [task](../tasks/twitter-search.md) |
| | [API gateway + rate limiter](api-gateway-rate-limiter-design.md) | [task](../tasks/api-gateway-rate-limiter.md) |
| | [Key-value store](key-value-store-design.md) 🔒 | [task](../tasks/key-value-store.md) |
| | [Job scheduler on Postgres](job-scheduler-postgres-deep-dive.md) | [task](../tasks/job-scheduler.md) |
| | [DAG pipeline orchestration](dag-pipeline-orchestration-design.md) 🔒 | [task](../tasks/dag-pipeline-orchestration.md) |
| | [Workflow orchestration](workflow-orchestration-design.md) 🔒 | [task](../tasks/workflow-orchestration.md) |
| Fintech | [IBKR-style trading platform](ibkr-trading-platform-design.md) 🔒 | [task](../tasks/ibkr-trading-platform.md) |
| Security | [FastAPI RBAC](fastapi-rbac-design.md) | [task](../tasks/fastapi-rbac.md) |
| ML systems | [Feature store](feature-store-design.md) | [task](../tasks/feature-store.md) |
| | [Recommendation system](recommendation-system-design.md) 🔒 | [task](../tasks/recommendation-system.md) |
| | [ML inference platform](ml-inference-platform-design.md) 🔒 | [task](../tasks/ml-inference-platform.md) |
| | [Parallel ML training](parallel-ml-training-design.md) 🔒 | [task](../tasks/parallel-ml-training.md) |
| AI / LLM systems | [LLM gateway](llm-gateway-design.md) | [task](../tasks/llm-gateway.md) |
| | [RAG platform](rag-platform-design.md) | [task](../tasks/rag-platform.md) |
| | [AI search engine](ai-search-engine-design.md) 🔒 | [task](../tasks/ai-search-engine.md) |
| | [Agent orchestration](agent-orchestration-design.md) | [task](../tasks/agent-orchestration.md) |
| | [AI agent platform](ai-agent-platform-design.md) | [task](../tasks/ai-agent-platform.md) |
| | [Tool platform](tool-platform-design.md) | [task](../tasks/tool-platform.md) |
| | [AI observability & evaluation](ai-observability-evaluation-design.md) | [task](../tasks/ai-observability-evaluation.md) |

## Pattern references

| Reference | Use it for |
|---|---|
| [API design patterns](api-design-patterns.md) | REST/gRPC resource design, pagination, versioning, errors |
| [API message patterns](api-message-patterns.md) | Sync vs. async messaging, events, sagas |
| [Big-tech API standards](big-tech-api-standards.md) | How Google, Stripe, Microsoft and others standardize APIs |
| [Database design best practices](database-design-best-practices.md) | Schema design, indexing, scaling from startup to enterprise |
| [Write-ahead log deep dive](write-ahead-log-deep-dive.md) | Building a WAL: durability, fsync, recovery |
| [Zero-GC Python](high-performance-python-zero-gc.md) | Low-latency Python services |

**Read a solution after attempting its task.** Solutions show one defensible design, not the
only one. Where you chose differently, check whether your choice still meets the task's
numbers.
