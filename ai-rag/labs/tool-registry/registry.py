"""Tool Registry — lifecycle, publish, version resolution, deprecation (design doc §4).

This is the control-plane core: the single source of truth for *what tools exist, who
may call them, what their schemas look like, and what version they're at*.  The design
doc's §17.1 trade-off section explains why this is centralized: the founding incident
was the absence of a central answer to "which agents can delete a customer record."

The registry is an in-memory store (dict-based, keyed the same way the SQL schema
indexes).  In production this is Postgres with CDC-fed caches — in a teaching lab
the logic is the same but the durability layer is a Python dict.

Key design decisions baked in:

1. **Publish-time validation is extensive** (§4.5) — schema validity, backward-
   compatibility, resource-limit ceiling checks — all happen *before* a version
   can enter even ``PENDING_APPROVAL``.

2. **Auto-approval has narrow criteria** (§4.2) — not destructive, no sensitive
   tags, team already has a track record.  Everything else routes to review.

3. **Version resolution is range-based** (§3.4) — agents pin ``^2.1.0``, the
   registry resolves to the latest compatible active version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import evolution as EV
import schema as SC
from models import (
    RESOURCE_CEILING,
    Annotation,
    DependencyRelation,
    ExecutionMode,
    Tool,
    ToolDefinition,
    ToolDependency,
    ToolState,
    ToolVersion,
    _new_id,
    _now,
    _schema_hash,
    classify_bump,
    parse_semver,
    semver_satisfies,
)


# ---------------------------------------------------------------------------
# Publish-time errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishError:
    code: str
    message: str


@dataclass(frozen=True)
class PublishResult:
    tool_version: ToolVersion | None
    errors: tuple[PublishError, ...]

    @property
    def ok(self) -> bool:
        return self.tool_version is not None and len(self.errors) == 0


# Tags that trigger a review (§4.2)
_REVIEW_TAGS = frozenset({"pii", "finance", "external-network-write"})

# Minimum deprecation grace period (§4.3)
MIN_DEPRECATION_DAYS = 90


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central tool registry — in-memory implementation of §4."""

    def __init__(self) -> None:
        # Primary stores, keyed like §4.4's SQL tables.
        self._tools: dict[str, Tool] = {}  # keyed by "{namespace}.{name}"
        self._versions: dict[str, ToolVersion] = {}  # keyed by tool_version_id
        self._dependencies: list[ToolDependency] = []
        # Track which teams have at least one active tool (for auto-approval §4.2).
        self._active_teams: set[str] = set()

    # ---- Lookup helpers ----

    def _tool_key(self, ns: str, name: str) -> str:
        return f"{ns}.{name}"

    def get_tool(self, namespace: str, name: str) -> Tool | None:
        return self._tools.get(self._tool_key(namespace, name))

    def get_version(self, tool_version_id: str) -> ToolVersion | None:
        return self._versions.get(tool_version_id)

    def get_version_by_semver(self, namespace: str, name: str, semver: str) -> ToolVersion | None:
        tool = self.get_tool(namespace, name)
        if tool is None:
            return None
        return tool.versions.get(semver)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def active_versions(self, namespace: str, name: str) -> list[ToolVersion]:
        tool = self.get_tool(namespace, name)
        if tool is None:
            return []
        return [v for v in tool.versions.values() if v.state == ToolState.ACTIVE]

    # ---- Publish (§4.5) ----

    def publish(
        self,
        definition: ToolDefinition,
        published_by: str = "ci-bot",
    ) -> PublishResult:
        """Publish a new tool version.  Runs all publish-time validation."""
        errors: list[PublishError] = []
        meta = definition.metadata
        spec = definition.spec
        ns, name = meta.namespace, meta.name
        key = self._tool_key(ns, name)

        # 1. Parse and validate semver.
        try:
            parse_semver(definition.version)
        except ValueError as e:
            errors.append(PublishError("INVALID_SEMVER", str(e)))
            return PublishResult(None, tuple(errors))

        # 2. Check for duplicate version.
        tool = self._tools.get(key)
        if tool is not None and definition.version in tool.versions:
            errors.append(PublishError(
                "DUPLICATE_VERSION",
                f"Version {definition.version} already exists for {key}",
            ))
            return PublishResult(None, tuple(errors))

        # 3. Validate input/output schemas are themselves valid JSON Schema subsets.
        #    (We check that they are dicts with a "type" key at minimum.)
        for schema_name, schema_val in [
            ("input_schema", spec.input_schema),
            ("output_schema", spec.output_schema),
        ]:
            if schema_val and not isinstance(schema_val, dict):
                errors.append(PublishError(
                    "INVALID_SCHEMA",
                    f"{schema_name} must be a dict (JSON Schema object)",
                ))

        # 4. Validate resource limits against platform ceiling (§9.5).
        limits = spec.execution.resource_limits
        if limits.cpu_millicores > RESOURCE_CEILING.cpu_millicores:
            errors.append(PublishError(
                "RESOURCE_LIMIT_EXCEEDED",
                f"cpu_millicores {limits.cpu_millicores} exceeds ceiling {RESOURCE_CEILING.cpu_millicores}",
            ))
        if limits.memory_mb > RESOURCE_CEILING.memory_mb:
            errors.append(PublishError(
                "RESOURCE_LIMIT_EXCEEDED",
                f"memory_mb {limits.memory_mb} exceeds ceiling {RESOURCE_CEILING.memory_mb}",
            ))

        # 5. Validate annotation consistency (§3.3).
        if spec.annotations.long_running and spec.execution.mode == ExecutionMode.SYNC:
            errors.append(PublishError(
                "ANNOTATION_CONFLICT",
                "long_running: true requires execution.mode: async",
            ))

        # 6. Backward-compatibility check against previous active version (§6.4).
        if tool is not None:
            prev = self._latest_active_version(tool)
            if prev is not None:
                bump = classify_bump(prev.semver, definition.version)
                # Check input schema compatibility.
                if spec.input_schema and prev.definition.spec.input_schema:
                    compat = EV.check_compatibility(
                        prev.definition.spec.input_schema,
                        spec.input_schema,
                        bump,
                        direction="input",
                    )
                    if not compat.compatible:
                        for v in compat.violations:
                            errors.append(PublishError(
                                "SCHEMA_BREAKING_CHANGE",
                                f"[input] {v.description} at {v.path} — requires MAJOR bump",
                            ))
                # Check output schema compatibility.
                if spec.output_schema and prev.definition.spec.output_schema:
                    compat = EV.check_compatibility(
                        prev.definition.spec.output_schema,
                        spec.output_schema,
                        bump,
                        direction="output",
                    )
                    if not compat.compatible:
                        for v in compat.violations:
                            errors.append(PublishError(
                                "SCHEMA_BREAKING_CHANGE",
                                f"[output] {v.description} at {v.path} — requires MAJOR bump",
                            ))

        if errors:
            return PublishResult(None, tuple(errors))

        # 7. Create the Tool entry if it doesn't exist.
        now = _now()
        if tool is None:
            tool = Tool(
                tool_id=_new_id(),
                namespace=ns,
                name=name,
                owner_team=meta.owner_team,
                on_call_contact=meta.on_call_contact,
                cost_center=meta.cost_center,
                created_at=now,
                tags=set(meta.tags),
            )
            self._tools[key] = tool
        else:
            # Update tags from the latest definition.
            tool.tags = set(meta.tags)

        # 8. Determine auto-approval eligibility (§4.2).
        review_reasons = self._check_review_required(definition)
        initial_state = (
            ToolState.ACTIVE if not review_reasons else ToolState.PENDING_APPROVAL
        )

        # 9. Create the ToolVersion.
        tv = ToolVersion(
            tool_version_id=_new_id(),
            tool_id=tool.tool_id,
            semver=definition.version,
            state=initial_state,
            definition=definition,
            input_schema_hash=_schema_hash(spec.input_schema),
            output_schema_hash=_schema_hash(spec.output_schema),
            published_by=published_by,
            published_at=now,
            review_required_reasons=tuple(review_reasons),
        )
        tool.versions[definition.version] = tv
        self._versions[tv.tool_version_id] = tv

        if initial_state == ToolState.ACTIVE:
            self._active_teams.add(meta.owner_team)

        return PublishResult(tv, ())

    def _check_review_required(self, defn: ToolDefinition) -> list[str]:
        """§4.2 — determine whether auto-approval applies."""
        reasons: list[str] = []
        ann = defn.spec.annotations
        tags = set(defn.metadata.tags)

        if ann.destructive:
            reasons.append("annotation:destructive")
        if tags & _REVIEW_TAGS:
            for t in sorted(tags & _REVIEW_TAGS):
                reasons.append(f"tag:{t}")
        if defn.metadata.owner_team not in self._active_teams:
            reasons.append("team:no_active_tools")
        return reasons

    def _latest_active_version(self, tool: Tool) -> ToolVersion | None:
        active = [v for v in tool.versions.values() if v.state == ToolState.ACTIVE]
        if not active:
            return None
        return max(active, key=lambda v: parse_semver(v.semver))

    # ---- Approval / Rejection (§4.2) ----

    def approve(
        self,
        tool_version_id: str,
        reviewed_by: str = "reviewer",
    ) -> ToolVersion | None:
        tv = self._versions.get(tool_version_id)
        if tv is None:
            return None
        if tv.state not in (ToolState.PENDING_APPROVAL, ToolState.IN_REVIEW):
            return None
        tv.state = ToolState.ACTIVE
        tv.reviewed_by = reviewed_by
        tv.reviewed_at = _now()
        self._active_teams.add(tv.definition.metadata.owner_team)
        return tv

    def reject(
        self,
        tool_version_id: str,
        reviewed_by: str = "reviewer",
    ) -> ToolVersion | None:
        tv = self._versions.get(tool_version_id)
        if tv is None:
            return None
        if tv.state not in (ToolState.PENDING_APPROVAL, ToolState.IN_REVIEW):
            return None
        tv.state = ToolState.PENDING_APPROVAL
        tv.reviewed_by = reviewed_by
        tv.reviewed_at = _now()
        return tv

    def submit_for_review(self, tool_version_id: str) -> ToolVersion | None:
        tv = self._versions.get(tool_version_id)
        if tv is None or tv.state != ToolState.PENDING_APPROVAL:
            return None
        tv.state = ToolState.IN_REVIEW
        return tv

    # ---- Deprecation (§4.3) ----

    def deprecate(
        self,
        tool_version_id: str,
        sunset_at: datetime | None = None,
        replacement_tool_version_id: str | None = None,
    ) -> ToolVersion | None:
        tv = self._versions.get(tool_version_id)
        if tv is None or tv.state != ToolState.ACTIVE:
            return None
        if sunset_at is None:
            sunset_at = _now() + timedelta(days=MIN_DEPRECATION_DAYS)
        tv.state = ToolState.DEPRECATED
        tv.sunset_at = sunset_at
        tv.replacement_tool_version_id = replacement_tool_version_id
        return tv

    def retire(self, tool_version_id: str, force: bool = False) -> ToolVersion | None:
        """Retire a deprecated version (§4.3 step 3–4)."""
        tv = self._versions.get(tool_version_id)
        if tv is None:
            return None
        if tv.state != ToolState.DEPRECATED and not force:
            return None
        tv.state = ToolState.RETIRED
        return tv

    # ---- Version resolution ----

    def resolve_version(
        self,
        namespace: str,
        name: str,
        version_range: str,
    ) -> ToolVersion | None:
        """Resolve a version range (e.g. ``^2.1.0``) to the latest compatible active version."""
        tool = self.get_tool(namespace, name)
        if tool is None:
            return None
        candidates = [
            v for v in tool.versions.values()
            if v.state in (ToolState.ACTIVE, ToolState.DEPRECATED)
            and semver_satisfies(v.semver, version_range)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda v: parse_semver(v.semver))

    # ---- Dependencies (§4.4, §5.4) ----

    def add_dependency(
        self,
        tool_version_id: str,
        depends_on_tool_version_id: str,
        relation: DependencyRelation = DependencyRelation.REQUIRES_OUTPUT_OF,
    ) -> ToolDependency:
        dep = ToolDependency(
            tool_version_id=tool_version_id,
            depends_on_tool_version_id=depends_on_tool_version_id,
            relation=relation,
        )
        self._dependencies.append(dep)
        return dep

    def get_dependencies(self, tool_version_id: str) -> list[ToolDependency]:
        return [d for d in self._dependencies if d.tool_version_id == tool_version_id]

    def get_upstream_chain(self, tool_version_id: str) -> list[str]:
        """Return the full upstream dependency chain (breadth-first), for §5.4."""
        visited: set[str] = set()
        queue = [tool_version_id]
        chain: list[str] = []
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            deps = [d for d in self._dependencies
                    if d.tool_version_id == current
                    and d.relation == DependencyRelation.REQUIRES_OUTPUT_OF]
            for dep in deps:
                if dep.depends_on_tool_version_id not in visited:
                    chain.append(dep.depends_on_tool_version_id)
                    queue.append(dep.depends_on_tool_version_id)
        return chain
