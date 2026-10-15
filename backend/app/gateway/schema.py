"""A small, bounded JSON-Schema subset used to validate model-produced tool arguments.

Why not ``jsonschema``: it is not an existing dependency and pulls several transitive
packages into a security-sensitive path. Tool parameter schemas here are server-authored
and tiny, so a bounded validator that FAILS CLOSED on any keyword it does not implement
(``$ref``, ``anyOf``, ``pattern`` ...) is smaller to review and cannot be steered into
regex or reference blow-ups by model output.
"""
from __future__ import annotations

import math
from typing import Any

MAX_SCHEMA_DEPTH = 6
MAX_ARGUMENT_DEPTH = 6
MAX_TOOL_ARGUMENT_BYTES = 8_192

_STRUCTURAL = {
    "type", "properties", "required", "additionalProperties", "enum", "const", "items",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
}  # fmt: skip
_ANNOTATIONS = {"title", "description", "default", "examples", "$schema"}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def schema_is_supported(schema: Any, depth: int = 0) -> bool:
    """True only if every keyword is implemented here. Used when a ToolSpec is built."""
    if not isinstance(schema, dict) or depth > MAX_SCHEMA_DEPTH:
        return False
    if not set(schema) <= _STRUCTURAL | _ANNOTATIONS:
        return False
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not names or not all(isinstance(n, str) and n in _TYPES for n in names):
            return False
    properties = schema.get("properties", {})
    names = declared if isinstance(declared, list) else [declared]
    if "object" in names or "properties" in schema:
        # Every object level is closed and consistent: the model cannot smuggle extra keys,
        # and a "required" entry must be something the schema actually declares.
        if schema.get("additionalProperties") is not False:
            return False
        if not isinstance(properties, dict) or not set(schema.get("required", [])) <= set(properties):
            return False
    if not isinstance(properties, dict) or not all(
        isinstance(k, str) and schema_is_supported(v, depth + 1) for k, v in properties.items()
    ):
        return False
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        return False
    if not isinstance(schema.get("additionalProperties", True), bool):
        return False
    if "items" in schema and not schema_is_supported(schema["items"], depth + 1):
        return False
    if "enum" in schema and not isinstance(schema["enum"], list):
        return False
    return all(
        isinstance(schema[k], (int, float)) and not isinstance(schema[k], bool)
        and math.isfinite(schema[k])
        for k in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems")
        if k in schema
    )


def json_depth(value: Any) -> int:
    """Nesting depth, computed iteratively so hostile input cannot recurse."""
    deepest = 0
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            deepest = max(deepest, depth)
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            deepest = max(deepest, depth)
            stack.extend((v, depth + 1) for v in item)
    return deepest


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, (int, float)) and not isinstance(value, bool)  # number


def same_json(a: Any, b: Any) -> bool:
    """Type-aware JSON equality: True is not 1 and False is not 0, at any depth."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    return bool(a == b)


def validate(schema: dict[str, Any], value: Any, depth: int = 0) -> bool:
    """Validate ``value`` against a schema that passed ``schema_is_supported``."""
    if depth > MAX_SCHEMA_DEPTH + 1:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False  # NaN and infinities never validate, whatever min/max say
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_is_type(value, n) for n in names):
            return False
    if "const" in schema and not same_json(value, schema["const"]):
        return False
    if "enum" in schema and not any(same_json(value, option) for option in schema["enum"]):
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(name not in value for name in schema.get("required", [])):
            return False
        if schema.get("additionalProperties", True) is False and set(value) - set(properties):
            return False
        for name, sub in properties.items():
            if name in value and not validate(sub, value[name], depth + 1):
                return False
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            return False
        if "items" in schema and not all(validate(schema["items"], v, depth + 1) for v in value):
            return False
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            return False
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            return False
    return True
