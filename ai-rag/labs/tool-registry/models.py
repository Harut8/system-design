"""Domain models for the Tool Platform registry (design doc §3–§4).

Every data structure in this module mirrors the design document's relational schema
(§4.4) and YAML tool definition (§3.2), expressed as frozen dataclasses.  No ORM, no
Postgres — the registry module uses in-memory dicts keyed by the same columns the SQL
schema indexes on.

Design choice: ``ToolDefinition`` is *not* stored as an opaque JSONB blob the way
the design doc's ``tool_versions.definition`` column does.  In a production system
that's the right call (schema-on-read, forward-compatible).  In a teaching lab,
structured fields are more legible and more testable than ``definition["spec"]["annotations"]["destructive"]``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ToolState(str, Enum):
    """§4.1 lifecycle states."""

    PENDING_APPROVAL = "pending_approval"
    IN_REVIEW = "in_review"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class RuntimeClass(str, Enum):
    HTTP = "http"
    SQL = "sql"
    CODE_EXEC = "code_exec"
    AGENT = "agent"


class DependencyRelation(str, Enum):
    REQUIRES_OUTPUT_OF = "requires_output_of"
    COMMONLY_PAIRED_WITH = "commonly_paired_with"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Annotation:
    """§3.3 — five boolean flags with platform-behaviour semantics.

    The defaults are the *safest* configuration, not the most convenient one.
    A tool author who forgets to set a flag gets the restrictive path, which is
    the correct direction for a default on a platform whose founding incident
    was an overly-permissive wrapper.
    """

    read_only: bool = False
    idempotent: bool = False
    destructive: bool = True  # safe default: assume destructive
    requires_approval: bool = True  # safe default: require review
    long_running: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff: str = "exponential_jitter"
    base_delay_ms: int = 200


@dataclass(frozen=True)
class ResourceLimits:
    """§9.5 ceilings.  Declared limits must not exceed the platform ceiling."""

    cpu_millicores: int = 250
    memory_mb: int = 128
    network_egress: tuple[str, ...] = ()  # empty = no network


# Platform-enforced ceilings (§9.5)
RESOURCE_CEILING = ResourceLimits(
    cpu_millicores=2000,
    memory_mb=2048,
    network_egress=(),  # the ceiling check is on cpu/mem only; egress is an allowlist
)


@dataclass(frozen=True)
class ExecutionConfig:
    """§3.2 ``spec.execution`` block."""

    mode: ExecutionMode = ExecutionMode.SYNC
    runtime: RuntimeClass = RuntimeClass.HTTP
    timeout_ms: int = 2000
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True)
class CredentialRef:
    """§7.1 — reference only, never secret material."""

    name: str
    type: str = "oauth2_client_credentials"
    scopes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Tool definition (§3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolMetadata:
    """The ``metadata`` block of a tool definition."""

    name: str
    namespace: str
    owner_team: str
    on_call_contact: str = ""
    cost_center: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    """The ``spec`` block — everything the LLM and the platform need."""

    description: str
    annotations: Annotation = field(default_factory=Annotation)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    credentials: tuple[CredentialRef, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    error_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDefinition:
    """The complete, publishable tool definition (§3.2).

    ``version`` is the *declared* semver string, not the registry's internal
    ``tool_version_id``.
    """

    metadata: ToolMetadata
    spec: ToolSpec
    version: str  # semver, e.g. "2.1.0"


# ---------------------------------------------------------------------------
# Registry entities (§4.4)
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return str(uuid.uuid4())


def _schema_hash(schema: dict[str, Any]) -> str:
    """Deterministic hash of a JSON Schema for evolution diffing."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ToolVersion:
    """One published version of a tool (mirrors ``tool_versions`` in §4.4)."""

    tool_version_id: str
    tool_id: str
    semver: str  # e.g. "2.1.0"
    state: ToolState
    definition: ToolDefinition
    input_schema_hash: str
    output_schema_hash: str
    published_by: str
    published_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    sunset_at: datetime | None = None
    replacement_tool_version_id: str | None = None

    # Derived at publish time — reasons auto-approval was denied
    review_required_reasons: tuple[str, ...] = ()


@dataclass
class Tool:
    """The namespace + name identity, owning its versions (mirrors ``tools`` in §4.4)."""

    tool_id: str
    namespace: str
    name: str
    owner_team: str
    on_call_contact: str
    cost_center: str
    created_at: datetime
    versions: dict[str, ToolVersion] = field(default_factory=dict)  # keyed by semver
    tags: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolDependency:
    """§4.4 ``tool_dependencies`` edge."""

    tool_version_id: str
    depends_on_tool_version_id: str
    relation: DependencyRelation


# ---------------------------------------------------------------------------
# Tool bundles (§5.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRef:
    """A reference inside a bundle: ``namespace.name@version_range``."""

    namespace: str
    name: str
    version_range: str  # e.g. "^2.1.0", "^1.0.0"


@dataclass(frozen=True)
class ToolBundle:
    """§5.5 — a curated collection an operator attaches to an agent in one step."""

    name: str
    tools: tuple[ToolRef, ...]
    default_allowlist: bool = True


# ---------------------------------------------------------------------------
# Principals (for discovery scoping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """An agent identity for authorization-scoped discovery (§5.1, §8)."""

    agent_id: str
    tenant_id: str = ""
    granted_scopes: frozenset[str] = frozenset()
    # Tool patterns this agent is allowed to use (e.g. "payments.*")
    allowed_tool_patterns: tuple[str, ...] = ()
    denied_tools: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Semver utilities
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(v: str) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` string."""
    m = _SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"Invalid semver: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def semver_satisfies(version: str, range_spec: str) -> bool:
    """Check if ``version`` satisfies a caret range (``^X.Y.Z``).

    ``^2.1.0`` means ``>=2.1.0`` and ``<3.0.0`` — accept any 2.x >= 2.1.0.
    """
    if not range_spec.startswith("^"):
        return version == range_spec  # exact match fallback
    floor = range_spec[1:]
    floor_maj, floor_min, floor_patch = parse_semver(floor)
    v_maj, v_min, v_patch = parse_semver(version)
    if v_maj != floor_maj:
        return False
    if (v_min, v_patch) < (floor_min, floor_patch):
        return False
    return True


def classify_bump(old: str, new: str) -> str:
    """Return 'major', 'minor', or 'patch' for a version bump."""
    o = parse_semver(old)
    n = parse_semver(new)
    if n[0] != o[0]:
        return "major"
    if n[1] != o[1]:
        return "minor"
    if n[2] != o[2]:
        return "patch"
    raise ValueError(f"Versions are identical: {old}")
