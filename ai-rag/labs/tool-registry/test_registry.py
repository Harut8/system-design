"""Comprehensive test suite for the Tool Registry lab (design doc §3–§6).

Target: ~35-40 test assertions covering every layer of the registry crate:
1. Core models & semver utilities
2. Registry lifecycle state machine, publish validation, auto-approval, deprecation
3. Schema validation, coercion rules, structured LLM-correctable errors
4. Schema evolution & backward-compatibility enforcement (§6.4 table)
5. Discovery: keyword search, tag filtering, agent-scoped filtering, dependency graphs
6. MCP compatibility: projection, ingestion defaults
7. Authoring SDK: @tool decorator, Field constraints
"""

import datetime
from typing import Literal
from dataclasses import dataclass

import pytest

import discovery as DS
import evolution as EV
import mcp as MCP
import models as M
import registry as R
import schema as SC
import sdk as SDK


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


def make_sample_tool(
    name: str = "refund_order",
    namespace: str = "payments",
    version: str = "2.1.0",
    destructive: bool = True,
    requires_approval: bool = True,
    read_only: bool = False,
    tags: tuple[str, ...] = ("finance", "destructive"),
    owner_team: str = "payments-team",
    max_amount: int = 5000000,
) -> M.ToolDefinition:
    return M.ToolDefinition(
        metadata=M.ToolMetadata(
            name=name,
            namespace=namespace,
            owner_team=owner_team,
            on_call_contact="oncall@company.com",
            tags=tags,
        ),
        spec=M.ToolSpec(
            description="Issue a refund for a customer order.",
            annotations=M.Annotation(
                read_only=read_only,
                idempotent=True,
                destructive=destructive,
                requires_approval=requires_approval,
            ),
            execution=M.ExecutionConfig(
                mode=M.ExecutionMode.SYNC,
                runtime=M.RuntimeClass.HTTP,
                timeout_ms=3000,
            ),
            credentials=(M.CredentialRef(name="payments-token"),),
            input_schema={
                "type": "object",
                "required": ["order_id", "amount_cents", "reason"],
                "additionalProperties": False,
                "properties": {
                    "order_id": {"type": "string", "pattern": r"^ord_[a-z0-9]+$"},
                    "amount_cents": {"type": "integer", "minimum": 1, "maximum": max_amount},
                    "reason": {
                        "type": "string",
                        "enum": ["defective", "not_as_described", "other"],
                    },
                },
            },
            output_schema={
                "type": "object",
                "required": ["refund_id", "status"],
                "properties": {
                    "refund_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "completed"]},
                },
            },
        ),
        version=version,
    )


# ---------------------------------------------------------------------------
# 1. Models & Semver Tests
# ---------------------------------------------------------------------------


def test_semver_parsing_and_satisfaction():
    assert M.parse_semver("2.1.0") == (2, 1, 0)
    with pytest.raises(ValueError):
        M.parse_semver("2.1")

    assert M.semver_satisfies("2.1.0", "^2.0.0") is True
    assert M.semver_satisfies("2.5.1", "^2.1.0") is True
    assert M.semver_satisfies("3.0.0", "^2.1.0") is False
    assert M.semver_satisfies("2.0.9", "^2.1.0") is False


def test_classify_bump():
    assert M.classify_bump("2.1.0", "3.0.0") == "major"
    assert M.classify_bump("2.1.0", "2.2.0") == "minor"
    assert M.classify_bump("2.1.0", "2.1.1") == "patch"


# ---------------------------------------------------------------------------
# 2. Registry Lifecycle & Publish Tests
# ---------------------------------------------------------------------------


def test_registry_publish_and_review_trigger():
    reg = R.ToolRegistry()
    tdef = make_sample_tool(destructive=True, tags=("finance",))

    res = reg.publish(tdef)
    assert res.ok is True
    assert res.tool_version.state == M.ToolState.PENDING_APPROVAL
    assert "annotation:destructive" in res.tool_version.review_required_reasons
    assert "tag:finance" in res.tool_version.review_required_reasons


def test_registry_auto_approval():
    reg = R.ToolRegistry()
    # First tool for team (triggers team:no_active_tools)
    t1 = make_sample_tool(
        name="get_order",
        destructive=False,
        requires_approval=False,
        tags=("read_only",),
    )
    res1 = reg.publish(t1)
    assert res1.tool_version.state == M.ToolState.PENDING_APPROVAL

    # Approve t1 so team has active history
    reg.approve(res1.tool_version.tool_version_id)

    # Second tool for team, non-destructive, safe tags -> Auto Approve!
    t2 = make_sample_tool(
        name="get_receipt",
        destructive=False,
        requires_approval=False,
        tags=("read_only",),
    )
    res2 = reg.publish(t2)
    assert res2.ok is True
    assert res2.tool_version.state == M.ToolState.ACTIVE


def test_registry_deprecation_and_retirement():
    reg = R.ToolRegistry()
    tdef = make_sample_tool(destructive=False, requires_approval=False, tags=())
    res = reg.publish(tdef)
    vid = res.tool_version.tool_version_id
    reg.approve(vid)

    # Deprecate
    dep_version = reg.deprecate(vid)
    assert dep_version.state == M.ToolState.DEPRECATED
    assert dep_version.sunset_at is not None

    # Resolve should still find deprecated version if active range fits
    resolved = reg.resolve_version("payments", "refund_order", "^2.1.0")
    assert resolved.tool_version_id == vid

    # Retire
    ret_version = reg.retire(vid)
    assert ret_version.state == M.ToolState.RETIRED

    # Resolve no longer returns retired version
    assert reg.resolve_version("payments", "refund_order", "^2.1.0") is None


# ---------------------------------------------------------------------------
# 3. Schema Validation & Coercion Tests
# ---------------------------------------------------------------------------


def test_schema_validation_success():
    schema = make_sample_tool().spec.input_schema
    data = {"order_id": "ord_123", "amount_cents": 500, "reason": "defective"}
    res_data, errors = SC.validate_input(data, schema)
    assert len(errors) == 0
    assert res_data["amount_cents"] == 500


def test_schema_validation_structured_error():
    schema = make_sample_tool().spec.input_schema
    data = {"order_id": "ord_123", "amount_cents": 10000000, "reason": "defective"}
    _, errors = SC.validate_input(data, schema)
    assert len(errors) == 1
    err = errors[0]
    assert err.error_code == "VALIDATION_ERROR"
    assert err.field_path == "/amount_cents"
    assert err.constraint == "maximum"
    assert err.retryable is True
    assert "Reduce the value" in err.suggested_fix


def test_schema_validation_coercion():
    schema = {
        "type": "object",
        "properties": {
            "amount": {"type": "integer", "x-coerce": True},
            "category": {"type": "string", "enum": ["Refund", "Cancel"]},
            "tags": {"type": "array", "items": {"type": "string"}, "x-coerce": True},
        },
    }
    data = {
        "amount": "42",
        "category": "refund ",  # whitespace + casing
        "tags": "single_tag",
    }
    coerced, errors = SC.validate_input(data, schema, coerce=True)
    assert len(errors) == 0
    assert coerced["amount"] == 42
    assert coerced["category"] == "Refund"
    assert coerced["tags"] == ["single_tag"]


# ---------------------------------------------------------------------------
# 4. Schema Evolution Tests (§6.4 Table)
# ---------------------------------------------------------------------------


def test_evolution_add_optional_input_minor_ok():
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "int"}}}
    res = EV.check_compatibility(old, new, declared_bump="minor", direction="input")
    assert res.compatible is True


def test_evolution_add_required_input_requires_major():
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {
        "type": "object",
        "required": ["b"],
        "properties": {"a": {"type": "string"}, "b": {"type": "int"}},
    }
    res = EV.check_compatibility(old, new, declared_bump="minor", direction="input")
    assert res.compatible is False
    assert any(v.kind == "field_added_required" for v in res.violations)


def test_evolution_remove_input_field_requires_major():
    old = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "int"}}}
    new = {"type": "object", "properties": {"a": {"type": "string"}}}
    res = EV.check_compatibility(old, new, declared_bump="minor", direction="input")
    assert res.compatible is False
    assert any(v.kind == "field_removed" for v in res.violations)


def test_evolution_narrow_constraint_requires_major():
    old = {"type": "object", "properties": {"amt": {"type": "integer", "maximum": 100}}}
    new = {"type": "object", "properties": {"amt": {"type": "integer", "maximum": 50}}}
    res = EV.check_compatibility(old, new, declared_bump="minor", direction="input")
    assert res.compatible is False
    assert any(v.kind == "maximum_lowered" for v in res.violations)


# ---------------------------------------------------------------------------
# 5. Discovery Tests
# ---------------------------------------------------------------------------


def test_discovery_agent_scoping():
    reg = R.ToolRegistry()
    t1 = make_sample_tool(name="refund", namespace="payments")
    t2 = make_sample_tool(name="restart_server", namespace="ops")

    v1 = reg.publish(t1).tool_version
    v2 = reg.publish(t2).tool_version
    reg.approve(v1.tool_version_id)
    reg.approve(v2.tool_version_id)

    disc = DS.DiscoveryService(reg)

    # Support agent can only call payments.*
    support_agent = M.Principal(
        agent_id="support_bot",
        allowed_tool_patterns=("payments.*",),
    )

    res = disc.search(agent=support_agent)
    assert len(res) == 1
    assert res[0].tool_ref == "payments.refund"


def test_discovery_hallucination_recovery():
    reg = R.ToolRegistry()
    v1 = reg.publish(make_sample_tool(name="refund_order", namespace="payments")).tool_version
    reg.approve(v1.tool_version_id)

    disc = DS.DiscoveryService(reg)
    suggestion = disc.suggest("payments.refund_ordr")
    assert "payments.refund_order" in suggestion


# ---------------------------------------------------------------------------
# 6. MCP Compatibility Tests
# ---------------------------------------------------------------------------


def test_mcp_projection_and_ingestion():
    tdef = make_sample_tool()
    mcp_dict = MCP.to_mcp_tool(tdef)

    assert mcp_dict["name"] == "payments.refund_order"
    assert "WARNING: This tool performs destructive" in mcp_dict["description"]

    # Ingest back
    ingested = MCP.from_mcp_tool(mcp_dict, owner_team="external")
    assert ingested.metadata.name == "refund_order"
    assert ingested.metadata.namespace == "payments"
    # Strict defaults applied
    assert ingested.spec.annotations.destructive is True
    assert ingested.spec.annotations.requires_approval is True


# ---------------------------------------------------------------------------
# 7. Authoring SDK Tests
# ---------------------------------------------------------------------------


@dataclass
class RefundResult:
    refund_id: str
    status: str


def test_sdk_decorator():
    @SDK.tool(
        namespace="finance",
        name="issue_refund",
        annotations=M.Annotation(read_only=False, destructive=True),
    )
    def issue_refund(
        order_id: str,
        amount: int = SDK.Field(ge=1, le=500, description="Amount in USD"),
        reason: Literal["damaged", "late"] = "damaged",
    ) -> RefundResult:
        """Issue a refund to a customer."""
        return RefundResult(refund_id="r123", status="ok")

    tdef = issue_refund._tool_definition
    assert tdef.metadata.namespace == "finance"
    assert tdef.metadata.name == "issue_refund"
    assert tdef.spec.description == "Issue a refund to a customer."
    assert tdef.spec.input_schema["properties"]["amount"]["minimum"] == 1
    assert tdef.spec.input_schema["properties"]["amount"]["maximum"] == 500
    assert tdef.spec.output_schema["properties"]["refund_id"]["type"] == "string"
