"""Schema evolution — backward-compatibility enforcement at publish time (design doc §6.4).

Given an old and new JSON Schema (both for a tool's input or output), this module
computes a structural diff and classifies each change against the compatibility table
in §6.4.  The publish pipeline calls ``check_compatibility`` to decide whether a
declared MINOR/PATCH bump is safe, or whether the author needs to bump to a new MAJOR.

The rules are asymmetric between input and output schemas because the "who is the
producer" question differs:

- **Input schema** — the *agent* (caller) is the producer.  Removing a required field
  is safe for the agent (it just stops sending it), but dangerous for the tool (it
  relied on it).  Adding a required field is dangerous for the agent (old callers don't
  know about it).  So changes are evaluated from the tool's (server's) perspective.

- **Output schema** — the *tool* is the producer.  Removing a field is dangerous for
  the agent (old consumers relied on it).  Adding an optional field is safe (consumers
  ignore unknown fields).  So changes are evaluated from the agent's (consumer's)
  perspective.

Both directions use the same ``check_compatibility`` function, parameterized by
``direction``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SchemaChange:
    """One atomic diff between two schemas."""

    path: str  # JSON pointer, e.g. "/properties/amount_cents"
    kind: str  # e.g. "field_added", "field_removed", "type_changed", etc.
    description: str
    breaking: bool  # True if this requires a MAJOR bump


@dataclass(frozen=True)
class CompatibilityResult:
    """The full result of a backward-compatibility check."""

    compatible: bool  # True if all changes are safe for the declared bump
    changes: tuple[SchemaChange, ...]
    violations: tuple[SchemaChange, ...]  # subset of changes that are breaking

    @property
    def violation_summary(self) -> str:
        if not self.violations:
            return "No violations."
        lines = [f"  - [{v.path}] {v.description}" for v in self.violations]
        return "Breaking changes (require MAJOR bump):\n" + "\n".join(lines)


def check_compatibility(
    old_schema: dict[str, Any],
    new_schema: dict[str, Any],
    declared_bump: str,  # "major", "minor", "patch"
    direction: str = "input",  # "input" or "output"
) -> CompatibilityResult:
    """Check whether ``new_schema`` is backward-compatible with ``old_schema``
    given the declared version bump.

    For MAJOR bumps, all changes are accepted (that's the point of a MAJOR bump).
    For MINOR/PATCH, any breaking change is a violation.
    """
    changes = _diff_schemas(old_schema, new_schema, "", direction)
    if declared_bump == "major":
        # All changes are accepted for a MAJOR bump.
        return CompatibilityResult(
            compatible=True,
            changes=tuple(changes),
            violations=(),
        )
    violations = tuple(c for c in changes if c.breaking)
    return CompatibilityResult(
        compatible=len(violations) == 0,
        changes=tuple(changes),
        violations=violations,
    )


def _diff_schemas(
    old: dict[str, Any],
    new: dict[str, Any],
    path: str,
    direction: str,
) -> list[SchemaChange]:
    """Recursively diff two JSON Schemas."""
    changes: list[SchemaChange] = []

    # --- Type change ---
    old_type = old.get("type")
    new_type = new.get("type")
    if old_type is not None and new_type is not None and old_type != new_type:
        changes.append(SchemaChange(
            path=path or "/",
            kind="type_changed",
            description=f"Type changed from '{old_type}' to '{new_type}'",
            breaking=True,  # always MAJOR, per §6.4
        ))
        return changes  # type change makes further property-level diffing meaningless

    # --- Properties (for objects) ---
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))

    # Fields added in new
    for field_name in sorted(set(new_props) - set(old_props)):
        field_path = f"{path}/properties/{field_name}"
        is_required = field_name in new_required
        if direction == "input":
            # Adding a required input field is breaking (old callers don't send it).
            # Adding an optional input field is safe.
            changes.append(SchemaChange(
                path=field_path,
                kind="field_added_required" if is_required else "field_added_optional",
                description=f"{'Required' if is_required else 'Optional'} field '{field_name}' added",
                breaking=is_required,
            ))
        else:
            # Adding any output field is safe (consumers ignore unknown fields).
            changes.append(SchemaChange(
                path=field_path,
                kind="field_added",
                description=f"Output field '{field_name}' added",
                breaking=False,
            ))

    # Fields removed in new
    for field_name in sorted(set(old_props) - set(new_props)):
        field_path = f"{path}/properties/{field_name}"
        # Removing a field is always breaking (regardless of direction).
        changes.append(SchemaChange(
            path=field_path,
            kind="field_removed",
            description=f"Field '{field_name}' removed",
            breaking=True,
        ))

    # Fields that exist in both — check for changes
    for field_name in sorted(set(old_props) & set(new_props)):
        field_path = f"{path}/properties/{field_name}"
        old_field = old_props[field_name]
        new_field = new_props[field_name]

        # Recurse into nested schemas
        sub_changes = _diff_schemas(old_field, new_field, field_path, direction)
        changes.extend(sub_changes)

    # Required set changes (field exists in both but required status changed)
    for field_name in sorted(set(old_props) & set(new_props)):
        field_path = f"{path}/properties/{field_name}"
        was_required = field_name in old_required
        now_required = field_name in new_required
        if not was_required and now_required and direction == "input":
            changes.append(SchemaChange(
                path=field_path,
                kind="field_made_required",
                description=f"Field '{field_name}' made required (was optional)",
                breaking=True,
            ))
        elif was_required and not now_required and direction == "input":
            changes.append(SchemaChange(
                path=field_path,
                kind="field_made_optional",
                description=f"Field '{field_name}' made optional (was required)",
                breaking=False,  # relaxing a constraint is safe for callers
            ))

    # --- Constraint changes on the same field ---
    _diff_constraints(old, new, path, changes)

    return changes


def _diff_constraints(
    old: dict[str, Any],
    new: dict[str, Any],
    path: str,
    changes: list[SchemaChange],
) -> None:
    """Check for constraint widening/narrowing on a single field."""
    # Enum changes
    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if old_enum is not None and new_enum is not None:
        old_set = set(old_enum)
        new_set = set(new_enum)
        added = new_set - old_set
        removed = old_set - new_set
        if removed:
            changes.append(SchemaChange(
                path=path,
                kind="enum_values_removed",
                description=f"Enum values removed: {sorted(removed)}",
                breaking=True,
            ))
        if added:
            changes.append(SchemaChange(
                path=path,
                kind="enum_values_added",
                description=f"Enum values added: {sorted(added)}",
                breaking=False,  # widening
            ))

    # Maximum changes
    old_max = old.get("maximum")
    new_max = new.get("maximum")
    if old_max is not None and new_max is not None:
        if new_max > old_max:
            changes.append(SchemaChange(
                path=path,
                kind="maximum_raised",
                description=f"Maximum raised from {old_max} to {new_max}",
                breaking=False,  # widening
            ))
        elif new_max < old_max:
            changes.append(SchemaChange(
                path=path,
                kind="maximum_lowered",
                description=f"Maximum lowered from {old_max} to {new_max}",
                breaking=True,  # narrowing — old callers may send values that were valid
            ))

    # Minimum changes
    old_min = old.get("minimum")
    new_min = new.get("minimum")
    if old_min is not None and new_min is not None:
        if new_min < old_min:
            changes.append(SchemaChange(
                path=path,
                kind="minimum_lowered",
                description=f"Minimum lowered from {old_min} to {new_min}",
                breaking=False,  # widening
            ))
        elif new_min > old_min:
            changes.append(SchemaChange(
                path=path,
                kind="minimum_raised",
                description=f"Minimum raised from {old_min} to {new_min}",
                breaking=True,  # narrowing
            ))

    # Pattern changes
    old_pattern = old.get("pattern")
    new_pattern = new.get("pattern")
    if old_pattern is not None and new_pattern is not None and old_pattern != new_pattern:
        changes.append(SchemaChange(
            path=path,
            kind="pattern_changed",
            description=f"Pattern changed from '{old_pattern}' to '{new_pattern}'",
            breaking=True,  # can't statically prove widening for regexes
        ))
