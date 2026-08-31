"""Multi-act report runner for the Tool Registry lab (`tool-platform-design.md` §3–§6).

Demonstrates all seven core capabilities of the registry crate in runnable terminal acts:

    python3 run.py            # run all acts
    python3 run.py --list     # list available act names
    python3 run.py publish validation evolution  # run specific acts

Acts:
  1. publish    — Lifecycle state machine & review triggers (§4.1, §4.2)
  2. validation — Schema validation, coercion, structured LLM-readable errors (§6)
  3. evolution  — Backward-compatibility checks & semver enforcement (§6.4)
  4. discovery  — Keyword search, tag filter, agent-scoped discovery (§5)
  5. mcp        — MCP projection, ingestion, and superset gap analysis (§3.6)
  6. sdk        — @tool decorator & signature schema derivation (§3.5)
  7. deprecate  — Sunset grace period & forced retirement (§4.3)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

import discovery as DS
import evolution as EV
import mcp as MCP
import models as M
import registry as R
import schema as SC
import sdk as SDK


def banner(title: str) -> None:
    print(f"\n{'=' * 75}\n  {title}\n{'=' * 75}")


def act_publish() -> None:
    banner("Act 1: Registry Publish & Review Workflow (§4.1, §4.2)")

    reg = R.ToolRegistry()

    # 1. Publish a high-risk payment refund tool
    refund_tool = M.ToolDefinition(
        metadata=M.ToolMetadata(
            name="refund_order",
            namespace="payments",
            owner_team="payments-team",
            on_call_contact="payments-oncall@company.com",
            tags=("finance", "destructive", "pii"),
        ),
        spec=M.ToolSpec(
            description="Issue a full or partial refund for a customer order.",
            annotations=M.Annotation(
                read_only=False,
                idempotent=True,
                destructive=True,
                requires_approval=True,
            ),
            execution=M.ExecutionConfig(
                mode=M.ExecutionMode.SYNC,
                runtime=M.RuntimeClass.HTTP,
                timeout_ms=3000,
            ),
            credentials=(M.CredentialRef(name="payments-token"),),
            input_schema={
                "type": "object",
                "required": ["order_id", "amount_cents"],
                "properties": {
                    "order_id": {"type": "string"},
                    "amount_cents": {"type": "integer", "minimum": 1},
                },
            },
        ),
        version="1.0.0",
    )

    res = reg.publish(refund_tool)
    tv = res.tool_version
    print(f"[Publish 1] payments.refund_order@1.0.0")
    print(f"  Status: {tv.state.value.upper()}")
    print(f"  Review Required Reasons: {list(tv.review_required_reasons)}")

    # Move through approval
    reg.submit_for_review(tv.tool_version_id)
    print(f"  After review submission: {tv.state.value.upper()}")
    reg.approve(tv.tool_version_id, reviewed_by="sec-admin@company.com")
    print(f"  After approval: {tv.state.value.upper()}")

    # 2. Publish a low-risk lookup tool from the same approved team (Auto-Approve!)
    lookup_tool = M.ToolDefinition(
        metadata=M.ToolMetadata(
            name="get_order",
            namespace="payments",
            owner_team="payments-team",
            tags=("read_only",),
        ),
        spec=M.ToolSpec(
            description="Look up order status by ID.",
            annotations=M.Annotation(
                read_only=True,
                idempotent=True,
                destructive=False,
                requires_approval=False,
            ),
            input_schema={
                "type": "object",
                "required": ["order_id"],
                "properties": {"order_id": {"type": "string"}},
            },
        ),
        version="1.0.0",
    )

    res2 = reg.publish(lookup_tool)
    print(f"\n[Publish 2] payments.get_order@1.0.0")
    print(f"  Status: {res2.tool_version.state.value.upper()} (Auto-Approved!)")
    print(f"  Review Required Reasons: {list(res2.tool_version.review_required_reasons)}")


def act_validation() -> None:
    banner("Act 2: Schema Validation & Coercion (§6.2, §6.3)")

    schema = {
        "type": "object",
        "required": ["order_id", "amount_cents", "reason"],
        "additionalProperties": False,
        "properties": {
            "order_id": {"type": "string", "pattern": r"^ord_[a-z0-9]+$"},
            "amount_cents": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50000,
                "x-coerce": True,
            },
            "reason": {
                "type": "string",
                "enum": ["defective", "not_as_described", "other"],
            },
        },
    }

    # Case A: Out of bounds value -> Structured LLM-readable error
    invalid_input = {
        "order_id": "ord_12345",
        "amount_cents": 999999,  # exceeds maximum 50000
        "reason": "defective",
    }
    _, errors = SC.validate_input(invalid_input, schema)
    print("[Validation Error Example]")
    for e in errors:
        print(f"  Code: {e.error_code}")
        print(f"  Field Path: {e.field_path}")
        print(f"  Message: {e.message}")
        print(f"  Constraint: {e.constraint}")
        print(f"  Suggested Fix for LLM: {e.suggested_fix}\n")

    # Case B: Bounded Coercion (String digits + Enum case folding)
    coercible_input = {
        "order_id": "ord_999 ",  # whitespace trimmed
        "amount_cents": "2500",  # string digit -> integer coerced via x-coerce
        "reason": "DEFECTIVE",  # case-folded enum match
    }
    coerced, c_errors = SC.validate_input(coercible_input, schema, coerce=True)
    print("[Coercion Example]")
    print(f"  Raw Input:     {invalid_input}")
    print(f"  Coerced Output: {coerced}")
    print(f"  Errors: {len(c_errors)}")


def act_evolution() -> None:
    banner("Act 3: Schema Evolution & Backward-Compatibility (§6.4)")

    old_schema = {
        "type": "object",
        "required": ["order_id"],
        "properties": {
            "order_id": {"type": "string"},
            "amount_cents": {"type": "integer", "maximum": 50000},
        },
    }

    # 1. Non-breaking MINOR change: add optional field, raise maximum
    minor_new = {
        "type": "object",
        "required": ["order_id"],
        "properties": {
            "order_id": {"type": "string"},
            "amount_cents": {"type": "integer", "maximum": 100000},
            "note": {"type": "string"},
        },
    }
    res_minor = EV.check_compatibility(old_schema, minor_new, declared_bump="minor", direction="input")
    print(f"[MINOR Bump v1.0.0 -> v1.1.0]")
    print(f"  Compatible? {res_minor.compatible}")
    for c in res_minor.changes:
        print(f"    - {c.description} (breaking={c.breaking})")

    # 2. Breaking MINOR change: add required field, lower maximum
    breaking_new = {
        "type": "object",
        "required": ["order_id", "currency"],  # new required field!
        "properties": {
            "order_id": {"type": "string"},
            "amount_cents": {"type": "integer", "maximum": 10000},  # lowered max!
            "currency": {"type": "string"},
        },
    }
    res_breaking = EV.check_compatibility(old_schema, breaking_new, declared_bump="minor", direction="input")
    print(f"\n[MINOR Bump v1.0.0 -> v1.2.0 (Attempting breaking change without MAJOR bump)]")
    print(f"  Compatible? {res_breaking.compatible}")
    print(res_breaking.violation_summary)


def act_discovery() -> None:
    banner("Act 4: Discovery, Scoping & Dependency Traversal (§5)")

    reg = R.ToolRegistry()

    # Tool A: get_customer_id
    t1 = M.ToolDefinition(
        metadata=M.ToolMetadata(name="get_customer_id", namespace="crm", owner_team="crm-team", tags=("read_only",)),
        spec=M.ToolSpec(description="Find customer ID by email", annotations=M.Annotation(read_only=True, destructive=False, requires_approval=False)),
        version="1.0.0",
    )
    v1 = reg.publish(t1).tool_version
    reg.approve(v1.tool_version_id)

    # Tool B: get_orders
    t2 = M.ToolDefinition(
        metadata=M.ToolMetadata(name="get_orders", namespace="orders", owner_team="orders-team", tags=("read_only",)),
        spec=M.ToolSpec(description="Get order history for customer ID", annotations=M.Annotation(read_only=True, destructive=False, requires_approval=False)),
        version="1.0.0",
    )
    v2 = reg.publish(t2).tool_version
    reg.approve(v2.tool_version_id)

    # Link B depends on A
    reg.add_dependency(v2.tool_version_id, v1.tool_version_id)

    disc = DS.DiscoveryService(reg)

    # Search with Support agent scope (granted orders.* and crm.*)
    support_principal = M.Principal(agent_id="support_bot", allowed_tool_patterns=("orders.*", "crm.*"))
    results = disc.search(query="customer order history", agent=support_principal)

    print("[Search Results for 'customer order history' (Scoped to support_bot)]")
    for r in results:
        print(f"  Tool: {r.tool_ref}@{r.version} | Score: {r.score} | Why: {r.why_matched}")
        if r.dependencies:
            print(f"    Dependencies: {r.dependencies}")

    # Search with Billing agent scope (only payments.*) -> 0 results
    billing_principal = M.Principal(agent_id="billing_bot", allowed_tool_patterns=("payments.*",))
    billing_results = disc.search(query="customer order history", agent=billing_principal)
    print(f"\n[Search Results for 'customer order history' (Scoped to billing_bot)]")
    print(f"  Results returned: {len(billing_results)} (Authorization applied BEFORE ranking!)")


def act_mcp() -> None:
    banner("Act 5: MCP Projection & Ingestion (§3.6)")

    # 1. Project platform ToolDefinition -> MCP tool descriptor
    tdef = R.ToolDefinition(
        metadata=M.ToolMetadata(name="cancel_order", namespace="shipping", owner_team="logistics", tags=("destructive",)),
        spec=M.ToolSpec(
            description="Cancel an in-flight shipping order.",
            annotations=M.Annotation(destructive=True, requires_approval=True),
            input_schema={"type": "object", "properties": {"shipment_id": {"type": "string"}}},
        ),
        version="1.0.0",
    )

    mcp_descriptor = MCP.to_mcp_tool(tdef)
    print("[Projected MCP Tool Descriptor]")
    print(f"  Name: {mcp_descriptor['name']}")
    print(f"  Description:\n    {mcp_descriptor['description'].replace(chr(10), chr(10) + '    ')}")
    print(f"  InputSchema: {mcp_descriptor['inputSchema']}")

    # 2. Ingest external MCP descriptor -> Platform ToolDefinition (with restrictive defaults)
    external_mcp = {
        "name": "salesforce.update_lead",
        "description": "Update lead stage in Salesforce CRM.",
        "inputSchema": {"type": "object", "properties": {"lead_id": {"type": "string"}}},
    }
    ingested = MCP.from_mcp_tool(external_mcp)
    print("\n[Ingested External MCP Tool]")
    print(f"  Namespace: {ingested.metadata.namespace}")
    print(f"  Name:      {ingested.metadata.name}")
    print(f"  Annotations (Defaulted to Restrictive):")
    print(f"    destructive:       {ingested.spec.annotations.destructive}")
    print(f"    requires_approval: {ingested.spec.annotations.requires_approval}")


@dataclass
class InventoryResult:
    sku: str
    available_count: int


def act_sdk() -> None:
    banner("Act 6: Authoring SDK @tool Decorator (§3.5)")

    @SDK.tool(
        namespace="warehouse",
        name="check_stock",
        annotations=M.Annotation(read_only=True, destructive=False, requires_approval=False),
        owner_team="inventory-team",
    )
    def check_stock(
        sku: str = SDK.Field(pattern=r"^SKU-[0-9]{4}$", description="Item SKU code"),
        location_id: int = SDK.Field(ge=1, le=100, description="Warehouse ID"),
    ) -> InventoryResult:
        """Check available item count in a specific warehouse location."""
        return InventoryResult(sku=sku, available_count=42)

    defn = check_stock._tool_definition
    print("[Derived ToolDefinition from Python Decorator]")
    print(f"  Namespace/Name: {defn.metadata.namespace}.{defn.metadata.name}")
    print(f"  Description:    {defn.spec.description}")
    print(f"  Input Schema Properties:")
    for prop, s in defn.spec.input_schema["properties"].items():
        print(f"    - {prop}: {s}")
    print(f"  Output Schema:")
    print(f"    Type: {defn.spec.output_schema['type']}")
    print(f"    Properties: {list(defn.spec.output_schema['properties'].keys())}")


def act_deprecate() -> None:
    banner("Act 7: Deprecation Lifecycle & Sunset Grace Period (§4.3)")

    reg = R.ToolRegistry()
    tdef = R.ToolDefinition(
        metadata=M.ToolMetadata(name="legacy_search", namespace="search", owner_team="search-team"),
        spec=M.ToolSpec(description="Old search v1", annotations=M.Annotation(read_only=True, destructive=False, requires_approval=False)),
        version="1.0.0",
    )
    v1 = reg.publish(tdef).tool_version
    reg.approve(v1.tool_version_id)

    print(f"[Initial State]: {v1.state.value.upper()}")

    # Deprecate with replacement
    reg.deprecate(v1.tool_version_id, replacement_tool_version_id="search.v2_id")
    print(f"[After Deprecation]: {v1.state.value.upper()}")
    print(f"  Sunset At: {v1.sunset_at.isoformat()}")

    # Retire
    reg.retire(v1.tool_version_id)
    print(f"[After Retirement]: {v1.state.value.upper()}")


# ---------------------------------------------------------------------------
# Runner Dispatch
# ---------------------------------------------------------------------------

ACTS = {
    "publish": act_publish,
    "validation": act_validation,
    "evolution": act_evolution,
    "discovery": act_discovery,
    "mcp": act_mcp,
    "sdk": act_sdk,
    "deprecate": act_deprecate,
}


def main():
    args = sys.argv[1:]
    if "--list" in args:
        print("Available acts:")
        for name in ACTS:
            print(f"  {name}")
        return

    to_run = [a for a in args if not a.startswith("-")]
    if not to_run:
        to_run = list(ACTS.keys())

    for act_name in to_run:
        if act_name in ACTS:
            ACTS[act_name]()
        else:
            print(f"Unknown act: {act_name}")
            sys.exit(1)


if __name__ == "__main__":
    main()
