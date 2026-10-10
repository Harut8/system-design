"""MCP (Model Context Protocol) compatibility: projection and ingestion (design doc §3.6).

Targets the MCP **2026-07-28** ``Tool`` object (see ``ai-rag/26-mcp-and-agent-protocols.md`` §6
and §10). The platform's tool definition is a **superset** of an MCP tool, and this module maps
between the two.

1. **Projection** (``to_mcp_tool``) emits a valid ``tools/list`` entry with ``name``, ``title``,
   ``description``, ``inputSchema``, ``outputSchema`` (when declared) and real ``annotations``
   (``readOnlyHint``, ``destructiveHint``, ``idempotentHint``, ``openWorldHint``). Platform-only
   fields with no MCP equivalent (``requires_approval``, ``long_running``) go in ``_meta`` under a
   platform-prefixed key. MCP-only clients ignore them.

2. **Ingestion** (``from_mcp_tool``) wraps an external MCP tool in a platform ``ToolDefinition``.
   The spec says clients MUST treat annotations from untrusted servers as untrusted, so ingested
   annotations stay at the **most restrictive** setting (``destructive: true,
   requires_approval: true``) whatever the server claims. The server's claims are kept separately
   (``claimed_annotations``) so a reviewer can see them and later diffs can catch a server whose
   claims change.

What is still platform-only, and why:

* **No per-tool error schema.** MCP reports tool failures as results with ``isError: true`` and
  free-form ``content``, so ``error_schema`` does not project.
* **Authorization stays in the platform.** MCP defines OAuth 2.1-based authorization for HTTP
  transports, but it authenticates a *client* to a *server* and forbids token passthrough. It does
  not decide whether *this agent run* may call *this tool with these arguments for this user*.
  Per-tool RBAC and credential injection therefore remain the platform's job (design doc §6).
"""

from __future__ import annotations

import re
from typing import Any

from models import (
    Annotation,
    ToolDefinition,
    ToolMetadata,
    ToolSpec,
)

PROTOCOL_VERSION = "2026-07-28"

# Spec §Tool Names: 1-128 chars, ASCII letters, digits, "_", "-" and "." only.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

# Reverse-DNS prefix for platform data carried in ``_meta`` (MCP reserves its own prefixes).
META_PREFIX = "io.example.tool-registry/"

# ToolAnnotations defaults from the 2026-07-28 schema, applied when a server omits a hint.
SPEC_HINT_DEFAULTS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False}


def mcp_tool_name(definition: ToolDefinition) -> str:
    """Return ``namespace.name``, rejecting names the spec says clients may refuse."""
    name = f"{definition.metadata.namespace}.{definition.metadata.name}"
    if not TOOL_NAME_RE.match(name):
        raise ValueError(f"tool name {name!r} violates MCP tool-name rules")
    return name


def to_mcp_tool(definition: ToolDefinition) -> dict[str, Any]:
    """Project a ``ToolDefinition`` to an MCP 2026-07-28 ``Tool`` object."""
    meta = definition.metadata
    spec = definition.spec
    ann = spec.annotations

    input_schema = spec.input_schema or EMPTY_INPUT_SCHEMA
    if input_schema.get("type") != "object":
        raise ValueError("MCP requires inputSchema to have type 'object' at the root")

    hints: dict[str, Any] = {"readOnlyHint": ann.read_only}
    # The spec defines destructive/idempotent hints as meaningful only when not read-only.
    if not ann.read_only:
        hints["destructiveHint"] = ann.destructive
        hints["idempotentHint"] = ann.idempotent
    # An empty egress allowlist means the tool cannot reach anything outside the platform.
    hints["openWorldHint"] = bool(spec.execution.resource_limits.network_egress)

    tool: dict[str, Any] = {
        "name": mcp_tool_name(definition),
        "title": meta.name.replace("_", " ").title(),
        "description": spec.description,
        "inputSchema": input_schema,
        "annotations": hints,
        "_meta": {
            f"{META_PREFIX}requiresApproval": ann.requires_approval,
            f"{META_PREFIX}longRunning": ann.long_running,
        },
    }
    if spec.output_schema:
        # A server that declares outputSchema MUST return conforming structuredContent.
        tool["outputSchema"] = spec.output_schema
    return tool


def claimed_annotations(mcp_tool: dict[str, Any]) -> dict[str, bool]:
    """Return the hints a server claims, with spec defaults filled in. For review only.

    Non-boolean values are ignored, so a malformed claim falls back to the spec default.
    """
    raw = mcp_tool.get("annotations") or {}
    return {
        key: raw[key] if isinstance(raw.get(key), bool) else default
        for key, default in SPEC_HINT_DEFAULTS.items()
    }


def from_mcp_tool(
    mcp_tool: dict[str, Any],
    *,
    owner_team: str = "unassigned",
    default_version: str = "1.0.0",
) -> ToolDefinition:
    """Ingest an MCP tool descriptor and wrap it in a platform ``ToolDefinition``.

    Annotations are untrusted, so they default to the **most restrictive** setting (§3.6):
    ``destructive=True``, ``requires_approval=True``, ``read_only=False``, ``idempotent=False``.
    A human owner relaxes them after review. ``outputSchema`` is ingested so the platform can
    validate ``structuredContent`` in results.
    """
    full_name = mcp_tool.get("name", "")
    if not TOOL_NAME_RE.match(full_name):
        raise ValueError(f"tool name {full_name!r} violates MCP tool-name rules")
    namespace, sep, name = full_name.partition(".")
    if not sep:
        namespace, name = "external", full_name

    return ToolDefinition(
        metadata=ToolMetadata(
            name=name,
            namespace=namespace,
            owner_team=owner_team,
            tags=("mcp-imported",),
        ),
        spec=ToolSpec(
            description=mcp_tool.get("description", ""),
            annotations=Annotation(
                read_only=False,
                idempotent=False,
                destructive=True,  # most restrictive default; server claims are untrusted
                requires_approval=True,  # most restrictive default
                long_running=False,
            ),
            input_schema=mcp_tool.get("inputSchema") or dict(EMPTY_INPUT_SCHEMA),
            output_schema=mcp_tool.get("outputSchema") or {},
            error_schema={},  # MCP has no per-tool error schema
        ),
        version=default_version,
    )


def mcp_round_trip_report(definition: ToolDefinition) -> dict[str, Any]:
    """Show what survives an MCP projection and what ingestion deliberately discards.

    Used by run.py to demonstrate the superset relationship.
    """
    mcp = to_mcp_tool(definition)
    back = from_mcp_tool(mcp, owner_team=definition.metadata.owner_team)

    preserved = {
        "name": mcp["name"] == f"{definition.metadata.namespace}.{definition.metadata.name}",
        "description": mcp["description"] == definition.spec.description,
        "inputSchema": back.spec.input_schema == mcp["inputSchema"],
        "outputSchema": back.spec.output_schema == definition.spec.output_schema,
        "annotations (as hints)": "annotations" in mcp,
    }
    lost = []
    if definition.spec.error_schema:
        lost.append("error_schema (MCP uses isError results, no per-tool error schema)")
    lost.append("requires_approval, long_running (carried in _meta, not trusted on ingestion)")
    lost.append("execution config (timeout, retry, resource limits)")
    lost.append("credentials")
    lost.append("ownership metadata (owner_team, on_call, cost_center)")
    lost.append("tags")

    defaulted = {
        "destructive": back.spec.annotations.destructive,
        "requires_approval": back.spec.annotations.requires_approval,
        "idempotent": back.spec.annotations.idempotent,
        "read_only": back.spec.annotations.read_only,
    }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "preserved": preserved,
        "lost": lost,
        "claimed_by_server": claimed_annotations(mcp),
        "defaulted_on_ingestion": defaulted,
    }
