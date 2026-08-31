"""MCP (Model Context Protocol) compatibility — projection and ingestion (design doc §3.6).

The platform's tool definition is a **superset** of an MCP tool descriptor.  This
module handles the two-way mapping:

1. **Projection** (``to_mcp_tool``): strip platform-specific metadata (auth, SLA,
   annotations) and produce a valid MCP ``tools/list`` entry.  MCP has no first-class
   output/error schema or annotations field — so the projection folds read-only/
   destructive signals into the description as a best-effort hint.

2. **Ingestion** (``from_mcp_tool``): wrap an external MCP tool descriptor in a
   platform ``ToolDefinition`` with annotations defaulted to the **most restrictive**
   setting (``destructive: true, requires_approval: true``).  An MCP tool is never
   auto-trusted into the catalog with dangerous defaults — a human owner must review
   and relax them.

The platform deliberately does *not* delegate authorization or credential injection to
the MCP server itself — MCP has no standardized authz/credop model, and the org's
mandate requires the platform to remain in the loop regardless of transport.
"""

from __future__ import annotations

from typing import Any

from models import (
    Annotation,
    ExecutionConfig,
    ToolDefinition,
    ToolMetadata,
    ToolSpec,
)


def to_mcp_tool(definition: ToolDefinition) -> dict[str, Any]:
    """Project a ``ToolDefinition`` down to a valid MCP ``tools/list`` entry.

    MCP tools have: name, description, inputSchema.
    Everything else (output schema, error schema, annotations, auth, SLA) is
    platform-specific and gets stripped.  The destructive/read-only signal is
    folded into the description as a best-effort hint for MCP-only clients.
    """
    meta = definition.metadata
    spec = definition.spec

    # Build the description with annotation hints (§3.6).
    desc = spec.description
    hints: list[str] = []
    if spec.annotations.read_only:
        hints.append("This tool is read-only and safe for speculative use.")
    if spec.annotations.destructive:
        hints.append("WARNING: This tool performs destructive/irreversible actions.")
    if spec.annotations.requires_approval:
        hints.append("This tool requires human approval before execution.")

    if hints:
        desc = desc.rstrip() + "\n\n" + " ".join(hints)

    return {
        "name": f"{meta.namespace}.{meta.name}",
        "description": desc,
        "inputSchema": spec.input_schema if spec.input_schema else {},
    }


def from_mcp_tool(
    mcp_tool: dict[str, Any],
    *,
    owner_team: str = "unassigned",
    default_version: str = "1.0.0",
) -> ToolDefinition:
    """Ingest an MCP tool descriptor and wrap it in a platform ``ToolDefinition``.

    Annotations default to the **most restrictive** setting (§3.6):
    - ``destructive: true``  (never assume safe without human review)
    - ``requires_approval: true``
    - ``read_only: false``
    - ``idempotent: false``

    These must be explicitly relaxed by a human owner after review.
    """
    name_parts = mcp_tool.get("name", "unknown.unknown").split(".", 1)
    if len(name_parts) == 2:
        namespace, name = name_parts
    else:
        namespace = "external"
        name = name_parts[0]

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
                destructive=True,  # most restrictive default
                requires_approval=True,  # most restrictive default
                long_running=False,
            ),
            input_schema=mcp_tool.get("inputSchema", {}),
            # MCP has no output or error schema — left empty.
            output_schema={},
            error_schema={},
        ),
        version=default_version,
    )


def mcp_round_trip_report(definition: ToolDefinition) -> dict[str, Any]:
    """Show what's preserved and what's lost in an MCP projection.

    Useful for the run.py report to demonstrate the superset relationship.
    """
    mcp = to_mcp_tool(definition)
    back = from_mcp_tool(mcp, owner_team=definition.metadata.owner_team)

    preserved = {
        "name": mcp["name"] == f"{definition.metadata.namespace}.{definition.metadata.name}",
        "description_present": bool(mcp["description"]),
        "inputSchema": mcp["inputSchema"] == definition.spec.input_schema,
    }
    lost = []
    if definition.spec.output_schema:
        lost.append("output_schema")
    if definition.spec.error_schema:
        lost.append("error_schema")
    lost.append("annotations (destructive, read_only, idempotent, requires_approval, long_running)")
    lost.append("execution config (timeout, retry, resource limits)")
    lost.append("credentials")
    lost.append("ownership metadata (on_call, cost_center)")
    lost.append("tags (except as folded into description)")

    defaulted = {
        "destructive": back.spec.annotations.destructive,
        "requires_approval": back.spec.annotations.requires_approval,
        "idempotent": back.spec.annotations.idempotent,
        "read_only": back.spec.annotations.read_only,
    }

    return {
        "preserved": preserved,
        "lost": lost,
        "defaulted_on_ingestion": defaulted,
    }
