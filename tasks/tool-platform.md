## System Design Task: Tool Platform for AI Agents

### Problem Statement

Design a **centralized Tool Platform** that AI agents use to interact with the
outside world — REST/gRPC APIs, internal databases, code execution sandboxes,
file systems, SaaS integrations (Slack, Jira, Salesforce), and other agents.

The company runs thousands of autonomous and semi-autonomous LLM agents across
hundreds of teams: customer-support agents that issue refunds, coding agents
that open pull requests, data agents that run SQL against production
warehouses, ops agents that restart services, and research agents that browse
the web. Historically each team wired its agents directly to whatever APIs
they needed — hardcoded credentials in prompts, no consistent input
validation, no audit trail, no way to answer "which agents can delete a
customer record" or "what did this agent actually do at 3 AM last Tuesday."

Two incidents forced the issue. First, an agent with an overly broad database
credential ran an `UPDATE` with no `WHERE` clause because a tool wrapper
happily forwarded a malformed LLM-generated argument straight to SQL. Second,
a credential embedded in a shared prompt template leaked into a different
team's agent, which then used it to call a billing API it had no business
touching. Leadership's mandate: **no agent may call anything without a
uniform interface that validates its inputs, authorizes the call, executes it
under bounded resources, and logs it — full stop.**

Hundreds of teams need to **publish** tools (as easily as writing a function
and a schema), and **hundreds of thousands of agent instances** need to
**discover and invoke** those tools through one consistent interface,
regardless of whether the underlying tool is a stateless HTTP API, a
long-running batch job, a database query, or a sandboxed code execution
environment. The platform sits in the critical path of production agent
traffic and must not become the bottleneck that makes teams route around it.

Assume this platform will be adopted org-wide and its tool-definition format
will be depended upon by hundreds of tool authors and thousands of agent
prompts — treat the interface as something you cannot casually break.

---

### Functional Requirements

Your design must support:

1. **Tool Interface Standard**

   * A declarative tool definition: name, description (used by LLMs for
     selection), input JSON Schema, output JSON Schema, error schema
   * Machine-readable **annotations**: `read_only`, `idempotent`,
     `destructive`, `requires_approval`, `long_running`
   * Support both **synchronous** (request/response) and **asynchronous**
     (submit/poll or callback) tool contracts in the same standard
   * Compatibility (or an explicit, justified deviation) with an existing
     open convention such as **MCP (Model Context Protocol)** or OpenAPI, so
     external tool authors are not asked to learn a bespoke format
   * Versioned definitions — a tool's schema must evolve without breaking
     agents mid-conversation

2. **Tool Registry**

   * Publish, update, deprecate, and retire tools
   * **Semantic versioning** per tool; multiple versions may be live
     simultaneously
   * Ownership metadata: owning team, on-call contact, cost center
   * Declared **SLA**: expected latency, availability, rate limits
   * Capability tags (e.g. `finance`, `pii`, `write`, `external-network`)
     used for both discovery and policy
   * An **approval workflow** before a tool becomes discoverable — new tools,
     and especially tools tagged `destructive` or touching regulated data,
     require sign-off before agents can find them

3. **Tool Discovery**

   * Keyword and **semantic search** ("find a tool that can look up a
     customer's order history") returning ranked, agent-consumable results
   * Filter by capability, owner, cost tier, latency SLA, required scopes
   * Tool **recommendations** scoped to what a given agent identity is
     already authorized to use — never surface a tool an agent cannot call
   * **Dependency graphs**: some tools require another tool's output as
     input (e.g. `get_customer_id` → `get_customer_orders`); discovery must
     expose these relationships
   * Curated **tool bundles/collections** teams can attach to an agent in one
     step (e.g. "customer-support-tier-1" bundle)

4. **Schema Validation**

   * Validate every tool call's arguments against the tool's input schema
     **before** execution; reject with a structured, LLM-readable error
     otherwise
   * Validate the tool's response against its output schema before returning
     it to the agent
   * Define **schema evolution rules**: what changes are backward compatible
     (additive optional fields) vs. breaking (removing/renarrowing a field),
     and how the platform enforces this on publish
   * Coercion rules for near-miss LLM output (e.g. `"42"` for an integer
     field) — decide how permissive validation is and where that line is

5. **Credential Management**

   * Tools declare the credentials/scopes they need; the platform injects
     them at execution time — **credentials never appear in the agent's
     context window or prompt**
   * Support static API keys, OAuth2 (client-credentials and
     authorization-code with human consent), and short-lived tokens
   * **Scoping**: a credential is bound to (agent identity × tenant × tool),
     not globally shared
   * Rotation of credentials with zero tool-invocation downtime
   * Revocation: an agent or team's access can be cut immediately

6. **Authorization**

   * RBAC (roles: tool owner, tool publisher, agent operator, auditor) layered
     with ABAC (time of day, data classification, request cost, tenant)
   * Per-agent **allowlist/denylist** of tools and tool versions
   * **Human-in-the-loop approval gates** for tools marked
     `requires_approval` or above a configurable cost/impact threshold, with
     a defined timeout and fallback if no human responds
   * A policy evaluation point that can answer "can agent A, on behalf of
     tenant T, invoke tool X version Y with these arguments, right now" in
     the hot path with low added latency

7. **Execution Engine**

   * Synchronous execution for fast tools; asynchronous execution
     (submit → poll or webhook callback) for slow/long-running tools
   * **Sandboxed execution** for arbitrary/untrusted code tools (containers,
     gVisor, or WASM) with no access beyond what's explicitly granted
   * Enforced **resource limits**: CPU, memory, wall-clock time, network
     egress allowlist, disk
   * **Parallel tool calls** from a single agent turn, with per-agent
     concurrency caps
   * Deterministic **deadline propagation**: a tool chain inherits and
     subdivides the caller's overall time budget

8. **Timeout and Retry**

   * Timeout configurable at platform default, per-tool, and per-call
     override, with the tightest applicable value winning
   * Retry policy per tool (exponential backoff + jitter), gated by the
     tool's `idempotent` annotation — non-idempotent tools are never silently
     retried
   * **Circuit breakers** per tool (and per downstream dependency) that trip
     on elevated error rate and shed load before a struggling dependency is
     hammered further
   * **Idempotency keys** for at-least-once delivery semantics on retries

9. **Result Handling**

   * Structured success/error results with a **stable error taxonomy**
     (validation error, auth error, timeout, downstream 5xx, quota exceeded,
     approval denied, etc.) so agents can branch on failure type
   * **Partial results** and progress updates for long-running tools
   * **Streaming** results for tools that produce output incrementally
     (e.g. large query results, long file reads)

10. **Audit and Compliance**

    * Every invocation logged: who (agent identity, on behalf of which
      human/tenant), what (tool, version, redacted arguments), when, why
      (originating task/conversation ID), and the outcome
    * **Immutable, append-only** audit trail, independently queryable from
      the hot path
    * Data-access tracking sufficient to answer "which tools touched PII for
      customer X in the last 90 days"
    * Support compliance reporting (SOC2-style access reviews, GDPR
      data-subject access requests) and defined retention periods

11. **Observability**

    * Per-tool metrics: latency percentiles, success/error rate, invocation
      volume, cost
    * Per-agent metrics: tool-call patterns, most-used tools, anomalous usage
      (e.g. sudden spike in a `destructive`-tagged tool)
    * Alerting on SLA breach, elevated error rate, and authorization-denial
      spikes (a leading indicator of prompt injection or misconfiguration)

---

### Non-Functional Requirements

1. **Scale**

   * **8,000+ tools** registered across **600+ teams**, average 4 live
     versions per tool
   * **50,000+ distinct agent identities** (deployments), issuing calls on
     behalf of millions of end-user sessions
   * Sustained **40,000 tool invocations/second** platform-wide; peak
     **120,000/sec**
   * Discovery/search: **2,000 queries/sec**

2. **Latency**

   * Platform-added overhead (auth + validation + routing, excluding the
     tool's own execution time) P50 ≤ **8 ms**, P99 ≤ **40 ms**
   * Synchronous tool invocation end-to-end (platform + typical downstream
     tool) P99 ≤ **800 ms** for tools declaring a "fast" SLA tier
   * Discovery/search P99 ≤ **150 ms**
   * Async tool submission acknowledgment P99 ≤ **100 ms**

3. **Availability**

   * Registry read path (discovery, schema fetch): **99.99%**
   * Execution path (invoke): **99.95%**, with graceful degradation (serve
     stale-but-valid cached policy/schema) if a dependency (auth service,
     credential vault) has a brief outage, rather than hard-failing every
     call
   * No single tool provider outage should degrade unrelated tools —
     failures must be isolated (bulkhead)

4. **Security**

   * Credentials must never be observable by the agent/LLM or logged in
     plaintext anywhere, including audit logs
   * Every invocation must be attributable to a specific agent identity and,
     where applicable, an end user / tenant
   * Sandbox escape must be treated as a P0; the design must state its
     containment assumptions explicitly

5. **Durability / Compliance**

   * Audit events: zero data loss, retained **≥ 400 days** (longer for
     regulated tenants), immutable
   * Credential rotation must be possible with **zero invocation downtime**

6. **Extensibility**

   * A new tool should be publishable by a team with no platform-team
     involvement for non-sensitive tools (self-service), while
     sensitive/destructive tools go through approval
   * The tool interface standard must be able to absorb new tool categories
     (e.g. multi-modal tools, streaming tools) without a breaking change to
     already-published tools

---

### Constraints and Assumptions

* Agents are LLM-driven; tool arguments originate from model output and must
  be treated as **untrusted input** requiring validation, not just
  serialization.
* Some tools wrap systems the platform does not own (third-party SaaS APIs,
  partner services) — the platform cannot assume it controls the downstream
  system's reliability or rate limits.
* Some tool authors will want MCP-compatible tools to work with minimal
  changes; full alignment with the MCP spec is desirable but you may deviate
  where the org's authz/audit requirements demand it — state where and why.
* Assume this platform will exist for years and outlive several generations
  of underlying model providers; the tool interface is the durable contract,
  not any particular agent framework.

---

### What You Should Deliver

1. Requirements clarification and explicit assumptions
2. High-level architecture: registry, discovery, schema validation, auth
   (RBAC/ABAC), credential vault integration, execution engine, audit
   pipeline, observability
3. The **tool interface standard** itself — concrete schema examples,
   annotations, versioning rules, MCP-compatibility stance
4. Registry design: data model, publish/version/deprecate lifecycle, approval
   workflow
5. Discovery design: search strategy (keyword + semantic), ranking,
   dependency graph, bundles
6. Schema validation pipeline: request validation, response validation,
   evolution/compatibility rules
7. Credential management: vault integration, OAuth flows, scoping, rotation
8. Authorization: policy model, evaluation engine, human-approval gates,
   allowlists
9. Execution engine: sync/async paths, sandboxing approach, resource limits,
   deadline propagation, parallel dispatch
10. Timeout/retry/circuit-breaker design with concrete thresholds
11. Result handling: error taxonomy, partial/streaming results
12. Audit system: event schema, storage, queryability, compliance reporting
13. Observability: dashboards, alerts, anomaly detection
14. Data models (schemas) for the core entities
15. API design for registry, invocation, discovery, and audit
16. Capacity estimates with the arithmetic shown, not just conclusions
17. Failure-mode walkthroughs and the trade-offs you accepted
18. An evolution path from v1 (bare registry) to a mature platform

---

### Expectations

* **Be concrete.** Show actual JSON Schema snippets, example tool
  definitions, SQL/protobuf schemas, and API signatures — not just prose
  describing that they exist.
* **Name real mechanisms.** OPA/Rego for policy, HashiCorp Vault or AWS
  Secrets Manager for credentials, gVisor/Firecracker/WASM for sandboxing,
  OpenTelemetry for tracing — and justify the choice over the alternatives.
* **Do the capacity math.** Invocation rate, registry size, audit log volume
  per day, storage/retention cost — as numbers with the calculation shown.
* **Treat the LLM as an adversarial/unreliable input source.** Malformed
  arguments, prompt-injected instructions to call disallowed tools, and
  hallucinated tool names are the normal case to design for, not an edge
  case.
* Prefer a design a **small platform team can operate** for hundreds of
  tenants over one that requires bespoke, high-touch onboarding per team.

---
