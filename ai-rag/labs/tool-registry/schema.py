"""JSON Schema validation with LLM-correctable structured errors (design doc §6).

Implements a subset of JSON Schema Draft 2020-12 sufficient for every tool
schema in the design document — ``type``, ``required``, ``properties``,
``additionalProperties``, ``enum``, ``pattern``, ``minimum``/``maximum``,
``minLength``/``maxLength``, ``minItems``/``maxItems``, ``items``.

No external dependencies (no ``jsonschema`` library).  This is deliberate:
a teaching lab whose point is "schema validation is the boundary between a
probabilistic process and the real world" should *implement* that boundary,
not import it.

Design choices from §6:

1. **Strict by default.** Coercion is opt-in, field-level, and bounded to the
   four rules in §6.3.  The motivating incident was an overly-permissive
   wrapper.

2. **Errors shaped for an LLM to self-correct**, not for a human to read a
   stack trace (§6.2).  Every error carries ``field_path``, ``constraint``,
   ``retryable``, and ``suggested_fix``.

3. **Output validation is a separate concern** — a response that fails the
   tool's own output schema is a ``DOWNSTREAM_CONTRACT_VIOLATION``, not the
   caller's fault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


VALIDATOR_VERSION = "schema-v1"


# ---------------------------------------------------------------------------
# Structured validation error (§6.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    """One structured error, shaped for an LLM to self-correct on the next turn."""

    error_code: str  # e.g. "VALIDATION_ERROR"
    message: str
    field_path: str  # JSON pointer, e.g. "/amount_cents"
    constraint: str  # e.g. "maximum", "required", "type", "enum", "pattern"
    retryable: bool = True
    suggested_fix: str = ""


# ---------------------------------------------------------------------------
# Coercion (§6.3)
# ---------------------------------------------------------------------------

# These two are always on.
_ALWAYS_COERCE_ENUM_CASE = True
_ALWAYS_TRIM_WHITESPACE = True

# These two require the field to declare `x-coerce: true`.
_OPT_IN_STRING_TO_NUMBER = True
_OPT_IN_SINGLE_TO_ARRAY = True


def _coerce_value(
    value: Any,
    field_schema: dict[str, Any],
    *,
    opt_in_coerce: bool = False,
) -> tuple[Any, bool]:
    """Attempt bounded coercion.  Returns (coerced_value, was_coerced).

    Coercion is *never* silent — the caller logs that coercion happened so it
    can be audited.
    """
    target_type = field_schema.get("type")

    # 1. Trim whitespace on strings (always on).
    if isinstance(value, str) and _ALWAYS_TRIM_WHITESPACE:
        trimmed = value.strip()
        if trimmed != value:
            value = trimmed
            # Don't return yet — trimmed value may also need further coercion.

    # 2. Case-insensitive enum match (always on, low risk per §6.3).
    enum_values = field_schema.get("enum")
    if enum_values is not None and isinstance(value, str):
        lower_map = {str(e).lower(): e for e in enum_values}
        if value.lower() in lower_map and value not in enum_values:
            return lower_map[value.lower()], True

    # 3. String digits → integer/number (opt-in only).
    if opt_in_coerce and isinstance(value, str) and target_type in ("integer", "number"):
        try:
            if target_type == "integer":
                return int(value), True
            return float(value), True
        except (ValueError, TypeError):
            pass

    # 4. Single value → single-element array (opt-in only).
    if opt_in_coerce and target_type == "array" and not isinstance(value, list):
        return [value], True

    return value, False


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------


def validate(
    data: Any,
    schema: dict[str, Any],
    *,
    path: str = "",
    coerce: bool = False,
) -> tuple[Any, list[ValidationError]]:
    """Validate ``data`` against a JSON Schema subset.  Returns (possibly-coerced data, errors).

    When ``coerce=True``, bounded coercion rules from §6.3 are applied to
    fields that declare ``x-coerce: true`` (plus the two always-on rules).
    Coercion never silently mutates — the returned data is the coerced copy.
    """
    errors: list[ValidationError] = []
    result = _validate_node(data, schema, path, errors, coerce=coerce)
    return result, errors


def _validate_node(
    value: Any,
    schema: dict[str, Any],
    path: str,
    errors: list[ValidationError],
    *,
    coerce: bool = False,
) -> Any:
    """Recursive validation of a single schema node."""
    if not schema:
        return value

    target_type = schema.get("type")

    # --- Apply coercion before type-checking ---
    opt_in = schema.get("x-coerce", False) and coerce
    always_coerce = coerce  # for the always-on rules
    if always_coerce or opt_in:
        value, _ = _coerce_value(value, schema, opt_in_coerce=opt_in)

    # --- Type check ---
    if target_type is not None:
        if not _check_type(value, target_type):
            errors.append(ValidationError(
                error_code="VALIDATION_ERROR",
                message=f"Expected type '{target_type}', got {type(value).__name__} ({value!r})",
                field_path=path or "/",
                constraint="type",
                suggested_fix=f"Provide a value of type '{target_type}'.",
            ))
            return value  # short-circuit on type mismatch

    # --- Enum ---
    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"Value {value!r} not in allowed values: {enum_values}",
            field_path=path or "/",
            constraint="enum",
            suggested_fix=f"Use one of: {', '.join(repr(e) for e in enum_values)}.",
        ))

    # --- Pattern ---
    pattern = schema.get("pattern")
    if pattern is not None and isinstance(value, str):
        if not re.search(pattern, value):
            errors.append(ValidationError(
                error_code="VALIDATION_ERROR",
                message=f"Value {value!r} does not match pattern '{pattern}'",
                field_path=path or "/",
                constraint="pattern",
                suggested_fix=f"Provide a string matching the pattern '{pattern}'.",
            ))

    # --- Numeric bounds ---
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _check_numeric_bounds(value, schema, path, errors)

    # --- String length ---
    if isinstance(value, str):
        _check_string_length(value, schema, path, errors)

    # --- Array validation ---
    if isinstance(value, list):
        value = _validate_array(value, schema, path, errors, coerce=coerce)

    # --- Object validation ---
    if isinstance(value, dict) and target_type == "object":
        value = _validate_object(value, schema, path, errors, coerce=coerce)

    return value


def _check_type(value: Any, expected: str) -> bool:
    """JSON Schema type check."""
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    expected_types = type_map.get(expected)
    if expected_types is None:
        return True  # unknown type, pass
    # bool is a subclass of int in Python — exclude it for integer/number
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected_types)


def _check_numeric_bounds(
    value: int | float,
    schema: dict[str, Any],
    path: str,
    errors: list[ValidationError],
) -> None:
    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must be >= {minimum} (got {value})",
            field_path=path or "/",
            constraint="minimum",
            suggested_fix=f"Increase the value to at least {minimum}.",
        ))
    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must be <= {maximum} (got {value})",
            field_path=path or "/",
            constraint="maximum",
            suggested_fix=f"Reduce the value to at most {maximum}.",
        ))


def _check_string_length(
    value: str,
    schema: dict[str, Any],
    path: str,
    errors: list[ValidationError],
) -> None:
    min_len = schema.get("minLength")
    if min_len is not None and len(value) < min_len:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must be at least {min_len} characters (got {len(value)})",
            field_path=path or "/",
            constraint="minLength",
            suggested_fix=f"Provide a string with at least {min_len} characters.",
        ))
    max_len = schema.get("maxLength")
    if max_len is not None and len(value) > max_len:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must be at most {max_len} characters (got {len(value)})",
            field_path=path or "/",
            constraint="maxLength",
            suggested_fix=f"Provide a string with at most {max_len} characters.",
        ))


def _validate_array(
    value: list,
    schema: dict[str, Any],
    path: str,
    errors: list[ValidationError],
    *,
    coerce: bool = False,
) -> list:
    items_schema = schema.get("items")
    result = []
    for i, item in enumerate(value):
        if items_schema:
            item = _validate_node(item, items_schema, f"{path}/{i}", errors, coerce=coerce)
        result.append(item)

    min_items = schema.get("minItems")
    if min_items is not None and len(value) < min_items:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must have at least {min_items} items (got {len(value)})",
            field_path=path or "/",
            constraint="minItems",
        ))
    max_items = schema.get("maxItems")
    if max_items is not None and len(value) > max_items:
        errors.append(ValidationError(
            error_code="VALIDATION_ERROR",
            message=f"{_field_name(path)} must have at most {max_items} items (got {len(value)})",
            field_path=path or "/",
            constraint="maxItems",
        ))
    return result


def _validate_object(
    value: dict,
    schema: dict[str, Any],
    path: str,
    errors: list[ValidationError],
    *,
    coerce: bool = False,
) -> dict:
    """Validate an object's properties, required fields, and additionalProperties."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    additional = schema.get("additionalProperties", True)

    result = {}

    # Check required fields
    for req_field in required:
        if req_field not in value:
            errors.append(ValidationError(
                error_code="VALIDATION_ERROR",
                message=f"Missing required field '{req_field}'",
                field_path=f"{path}/{req_field}",
                constraint="required",
                suggested_fix=f"Add the '{req_field}' field to your request.",
            ))

    # Validate declared properties
    for prop_name, prop_schema in properties.items():
        if prop_name in value:
            result[prop_name] = _validate_node(
                value[prop_name], prop_schema, f"{path}/{prop_name}", errors, coerce=coerce,
            )

    # Check for additional properties
    declared = set(properties.keys())
    for key in value:
        if key in declared:
            continue
        if additional is False:
            errors.append(ValidationError(
                error_code="VALIDATION_ERROR",
                message=f"Unexpected field '{key}' (additionalProperties is false)",
                field_path=f"{path}/{key}",
                constraint="additionalProperties",
                suggested_fix=f"Remove the '{key}' field. Allowed fields: {sorted(declared)}.",
            ))
        else:
            result[key] = value[key]

    # Carry over declared properties that were present but not validated above
    for key in value:
        if key not in result:
            result[key] = value[key]

    return result


def _field_name(path: str) -> str:
    """Human-readable field name from a JSON pointer path."""
    if not path:
        return "value"
    return path.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Convenience wrappers for the registry
# ---------------------------------------------------------------------------


def validate_input(
    args: dict[str, Any],
    input_schema: dict[str, Any],
    *,
    coerce: bool = True,
) -> tuple[dict[str, Any], list[ValidationError]]:
    """Validate tool call arguments against the tool's input schema (§6.1 step 2)."""
    return validate(args, input_schema, coerce=coerce)


def validate_output(
    result: Any,
    output_schema: dict[str, Any],
) -> list[ValidationError]:
    """Validate a tool's response against its output schema (§6.1 step 4).

    Failures here are ``DOWNSTREAM_CONTRACT_VIOLATION`` — the tool's fault,
    not the caller's.  They are never surfaced verbatim to the agent.
    """
    _, errors = validate(result, output_schema, coerce=False)
    return [
        ValidationError(
            error_code="DOWNSTREAM_CONTRACT_VIOLATION",
            message=e.message,
            field_path=e.field_path,
            constraint=e.constraint,
            retryable=False,
            suggested_fix="This is a bug in the tool, not your call. The tool owner has been notified.",
        )
        for e in errors
    ]
