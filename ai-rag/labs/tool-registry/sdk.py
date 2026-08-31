"""Authoring SDK — ``@tool(...)`` decorator that generates a ``ToolDefinition`` (design doc §3.5).

The goal: most tool authors should never hand-write the YAML in §3.2.  This thin
SDK generates a ``ToolDefinition`` from a type-annotated Python function, keeping the
JSON Schema and the actual handler signature from drifting apart.

Design decisions:

1. ``input_schema`` is derived from the function signature's type annotations.  This
   lab supports ``str``, ``int``, ``float``, ``bool``, ``list``, ``dict``, and
   ``Literal[...]`` from ``typing``.

2. ``description`` comes from the docstring — the same docstring the LLM sees at
   selection time.  This is a forcing function for tool authors to write
   selection-quality prose rather than implementation comments.

3. ``publish()`` is a separate, explicit step — the SDK never auto-publishes on
   import (§3.5's last paragraph).

4. ``Field(...)`` constraints (``ge``, ``le``, ``min_length``, ``max_length``,
   ``pattern``) map directly to JSON Schema keywords.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_args, get_origin, get_type_hints

from models import (
    Annotation,
    CredentialRef,
    ExecutionConfig,
    ToolDefinition,
    ToolMetadata,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Field constraints (analogous to Pydantic's Field)
# ---------------------------------------------------------------------------


# Sentinel for "no default"
_MISSING = object()


@dataclass
class Field:
    """Per-field constraints that map to JSON Schema keywords."""

    description: str = ""
    ge: int | float | None = None  # minimum (>=)
    le: int | float | None = None  # maximum (<=)
    gt: int | float | None = None  # exclusiveMinimum (>)
    lt: int | float | None = None  # exclusiveMaximum (<)
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    default: Any = _MISSING
    coerce: bool = False  # maps to x-coerce

    def to_schema_constraints(self) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        if self.description:
            constraints["description"] = self.description
        if self.ge is not None:
            constraints["minimum"] = self.ge
        if self.le is not None:
            constraints["maximum"] = self.le
        if self.gt is not None:
            constraints["exclusiveMinimum"] = self.gt
        if self.lt is not None:
            constraints["exclusiveMaximum"] = self.lt
        if self.min_length is not None:
            constraints["minLength"] = self.min_length
        if self.max_length is not None:
            constraints["maxLength"] = self.max_length
        if self.pattern is not None:
            constraints["pattern"] = self.pattern
        if self.coerce:
            constraints["x-coerce"] = True
        return constraints


# Sentinel for "no default"
_MISSING = Field.default


# ---------------------------------------------------------------------------
# Type → JSON Schema mapping
# ---------------------------------------------------------------------------


def _type_to_schema(annotation: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}

    origin = get_origin(annotation)

    # Literal["a", "b", "c"] → enum
    if origin is Literal:
        values = list(get_args(annotation))
        # Infer type from the first value.
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        if all(isinstance(v, int) for v in values):
            return {"type": "integer", "enum": values}
        return {"enum": values}

    # list[X] → array with items
    if origin is list:
        args = get_args(annotation)
        if args:
            return {"type": "array", "items": _type_to_schema(args[0])}
        return {"type": "array"}

    # dict[str, X] → object
    if origin is dict:
        return {"type": "object"}

    # Optional[X] → just use X's schema (required-ness is handled separately)
    if origin is typing.Union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])

    # Fallback
    return {"type": "string"}


# ---------------------------------------------------------------------------
# Return type → output schema
# ---------------------------------------------------------------------------


def _return_type_to_schema(annotation: Any) -> dict[str, Any]:
    """Convert a return type annotation to an output JSON Schema."""
    if annotation is None or annotation is inspect.Parameter.empty:
        return {}

    # If it's a dataclass, enumerate its fields.
    if hasattr(annotation, "__dataclass_fields__"):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for fname, fld in annotation.__dataclass_fields__.items():
            properties[fname] = _type_to_schema(fld.type)
            required.append(fname)
        return {
            "type": "object",
            "required": required,
            "properties": properties,
        }

    # Simple type
    return _type_to_schema(annotation)


# ---------------------------------------------------------------------------
# The @tool decorator
# ---------------------------------------------------------------------------


def tool(
    *,
    namespace: str,
    name: str,
    annotations: Annotation | None = None,
    credentials: list[str] | None = None,
    timeout_ms: int = 2000,
    tags: tuple[str, ...] = (),
    owner_team: str = "",
    version: str = "1.0.0",
) -> Callable:
    """Decorator that attaches a ``ToolDefinition`` to a function.

    Usage::

        @tool(namespace="payments", name="refund_order", ...)
        def refund_order(order_id: str, amount_cents: int) -> RefundResult:
            '''Issue a full or partial refund for a completed order.'''
            ...

    The generated ``ToolDefinition`` is available as ``fn._tool_definition``.
    """
    def decorator(fn: Callable) -> Callable:
        defn = derive_definition(
            fn,
            namespace=namespace,
            name=name,
            annotations=annotations,
            credentials=credentials,
            timeout_ms=timeout_ms,
            tags=tags,
            owner_team=owner_team,
            version=version,
        )
        fn._tool_definition = defn  # type: ignore[attr-defined]
        return fn
    return decorator


def derive_definition(
    fn: Callable,
    *,
    namespace: str,
    name: str,
    annotations: Annotation | None = None,
    credentials: list[str] | None = None,
    timeout_ms: int = 2000,
    tags: tuple[str, ...] = (),
    owner_team: str = "",
    version: str = "1.0.0",
) -> ToolDefinition:
    """Derive a ``ToolDefinition`` from a function's signature and docstring."""
    if annotations is None:
        annotations = Annotation()

    # --- Input schema from signature ---
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = hints.get(param_name, str)
        field_schema = _type_to_schema(annotation)

        # Check if the default is a Field instance with constraints.
        if isinstance(param.default, Field):
            field_schema.update(param.default.to_schema_constraints())
            if param.default.default is _MISSING:
                required.append(param_name)
        elif param.default is inspect.Parameter.empty:
            required.append(param_name)

        properties[param_name] = field_schema

    input_schema: dict[str, Any] = {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": properties,
    }

    # --- Output schema from return type ---
    return_annotation = hints.get("return", None)
    output_schema = _return_type_to_schema(return_annotation)

    # --- Description from docstring ---
    description = inspect.getdoc(fn) or ""

    # --- Credentials ---
    cred_refs = tuple(CredentialRef(name=c) for c in (credentials or []))

    return ToolDefinition(
        metadata=ToolMetadata(
            name=name,
            namespace=namespace,
            owner_team=owner_team,
            tags=tags,
        ),
        spec=ToolSpec(
            description=description,
            annotations=annotations,
            execution=ExecutionConfig(timeout_ms=timeout_ms),
            credentials=cred_refs,
            input_schema=input_schema,
            output_schema=output_schema,
        ),
        version=version,
    )
