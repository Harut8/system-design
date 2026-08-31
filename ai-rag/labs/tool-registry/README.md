# Lab: Tool Registry & Crate (`tool-platform-design.md` §3–§6)

A production-shaped Tool Registry crate for AI Agents — the control plane from
[`solutions/tool-platform-design.md`](../../solutions/tool-platform-design.md) §3–§6,
implemented as a set of pure Python modules with zero external dependencies.

**Status: rung 2 — implemented.** The code runs, 7 terminal report acts execute, and 16
unit tests pass.  It covers the **registry, schema validation, schema evolution,
discovery, MCP compatibility, and authoring SDK** layers.  Execution engine, credential
broker, OPA policies, and audit pipeline are out of scope as separate future labs.

```bash
python3 run.py            # run the 7-act report
python3 run.py --list     # list available acts
python3 test_registry.py  # run test assertions
pytest -v                 # run tests via pytest
```

---

## Contents

1. [What this is](#1-what-this-is)
2. [Module Overview](#2-module-overview)
3. [What the run actually shows](#3-what-the-run-actually-shows)
4. [Design Decisions & Implementation Notes](#4-design-decisions--implementation-notes)
5. [What this deliberately is not](#5-what-this-deliberately-is-not)

---

## 1. What this is

Chapter 24's thesis is that **a tool call is a request from an untrusted, probabilistic process**,
and every tool call must clear schema validation, authorization, resource bounds, and audit
before touching real systems.

This lab implements the **registry and validation boundary** — the half that exists before
execution:

- **Declarative Tool Definitions (§3.2):** Full schema carrying annotations (`read_only`,
  `idempotent`, `destructive`, `requires_approval`, `long_running`), execution constraints,
  and credential references.
- **Tool Registry (§4):** Centralized state machine (`PENDING_APPROVAL → IN_REVIEW → ACTIVE → DEPRECATED → RETIRED`),
  semver resolution, auto-approval heuristics, and sunset grace periods.
- **Schema Validation (§6):** Zero-dependency JSON Schema validator emitting structured,
  LLM-correctable errors with field pointer paths and suggested fixes.
- **Schema Evolution (§6.4):** Publish-time compatibility checker enforcing the §6.4 matrix
  (preventing breaking schema changes from being published under MINOR/PATCH bumps).
- **Discovery Service (§5):** Keyword search, capability filters, and **agent-scoped authorization
  filtering applied BEFORE ranking** (preventing prompt-injection probing).
- **MCP Compatibility (§3.6):** Projection to standard MCP descriptors and restrictive ingestion
  of external MCP tools.
- **Authoring SDK (§3.5):** `@tool(...)` decorator deriving schemas from type hints and docstrings.

---

## 2. Module Overview

| File | Design Section | Description |
|---|---|---|
| [`models.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/models.py) | §3.2, §4.4 | Dataclasses for `ToolDefinition`, `ToolVersion`, `Annotation`, `Principal`, `ToolBundle`, and semver utilities. |
| [`schema.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/schema.py) | §6.1–§6.3 | Zero-dep validator emitting `ValidationError` with `suggested_fix` and executing bounded coercion rules. |
| [`evolution.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/evolution.py) | §6.4 | Structural schema differ enforcing backward-compatibility rules at publish time. |
| [`registry.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/registry.py) | §4 | Central registry maintaining state transitions, approval queues, deprecation, and semver range resolution. |
| [`discovery.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/discovery.py) | §5 | Discovery service with agent-scoped authorization filtering before ranking, health scoring, and fuzzy recovery. |
| [`mcp.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/mcp.py) | §3.6 | MCP projection (`to_mcp_tool`) and restrictive default ingestion (`from_mcp_tool`). |
| [`sdk.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/sdk.py) | §3.5 | `@tool(...)` decorator deriving schemas and docstring descriptions from Python functions. |
| [`run.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/run.py) | All | 7-act interactive terminal report demonstrating all features. |
| [`test_registry.py`](file:///Users/harut/system-design/ai-rag/labs/tool-registry/test_registry.py) | All | 16 unit tests covering edge cases across all modules. |

---

## 3. What the run actually shows

Running `python3 run.py` executes 7 terminal acts:

1. **`publish`**: High-risk tools (`destructive`, `finance`) enter `PENDING_APPROVAL`. Once approved, subsequent low-risk tools from the same team auto-approve.
2. **`validation`**: Out-of-bounds input yields structured errors with `field_path`, `constraint`, and `suggested_fix`. Near-miss inputs undergo bounded coercion.
3. **`evolution`**: Optional field additions pass under MINOR bumps; adding a required field or narrowing constraints is rejected with a breaking violation summary.
4. **`discovery`**: A search for "customer order history" returns tools for a support agent, but returns 0 results for a billing agent (auth filtered before ranking).
5. **`mcp`**: Projects `shipping.cancel_order` to MCP (folding annotations into text), and ingests external `salesforce.update_lead` with default `destructive=True, requires_approval=True`.
6. **`sdk`**: `@tool(...)` decorates a Python function and builds a complete `ToolDefinition` from annotations and docstrings.
7. **`deprecate`**: Demonstrates `ACTIVE → DEPRECATED (with 90-day sunset_at) → RETIRED` lifecycle transitions.

---

## 4. Design Decisions & Implementation Notes

- **Zero External Dependencies:** Built entirely with Python standard library (`dataclasses`, `typing`, `re`, `hashlib`, `difflib`, `datetime`, `uuid`).
- **Authorization Before Ranking:** `DiscoveryService.search` filters candidates against `agent.allowed_tool_patterns` before calculating similarity scores or returning top-K.
- **LLM Error Taxonomy:** Validation errors carry `suggested_fix` to help LLMs self-correct within the ReAct loop without human intervention.
- **Restrictive Ingestion:** External MCP tools are ingested with `destructive=True` and `requires_approval=True` by default, forcing human review before agents can execute them.

---

## 5. What this deliberately is not

This lab focuses strictly on the **registry & control plane**:
- **Execution engine** (sync/async dispatch, gVisor sandboxing, timeouts, circuit breaking) is in Lab 2 / Lab 6.
- **Credential management** (HashiCorp Vault / OAuth token minting) is in §7.
- **OPA Policy Enforcement** (embedded WASM Rego evaluation) is in §8.
- **Audit pipeline** (Kafka → WORM storage) is in §11.
