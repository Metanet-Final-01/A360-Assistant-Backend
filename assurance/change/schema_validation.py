"""Small fail-closed validator for the JSON Schema keywords used by assurance policy."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$defs",
    "$ref",
    "title",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "minProperties",
    "items",
    "minItems",
    "uniqueItems",
    "minLength",
    "minimum",
    "format",
    "pattern",
    "maxItems",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "contains",
}


class SchemaValidationError(ValueError):
    """Raised when an instance or schema cannot be validated without ambiguity."""


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"unsupported schema reference: {reference}")
    current: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise SchemaValidationError(f"unresolved schema reference: {reference}")
        current = current[token]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"schema reference is not an object: {reference}")
    return current


def _matches_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected not in checks:
        raise SchemaValidationError(f"unsupported schema type: {expected}")
    return checks[expected](value)


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    unsupported = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unsupported:
        raise SchemaValidationError(
            f"{path}: schema uses unsupported keywords: {', '.join(unsupported)}"
        )
    if "$ref" in schema:
        _validate(value, _resolve_ref(root, schema["$ref"]), root, path)
        return
    for index, branch in enumerate(schema.get("allOf", [])):
        _validate(value, branch, root, f"{path}.allOf[{index}]")
    if "anyOf" in schema:
        matches = 0
        for branch in schema["anyOf"]:
            try:
                _validate(value, branch, root, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches == 0:
            raise SchemaValidationError(f"{path}: value does not match any allowed schema")
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _validate(value, branch, root, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(f"{path}: value must match exactly one allowed schema")
    if "not" in schema:
        try:
            _validate(value, schema["not"], root, path)
        except SchemaValidationError:
            pass
        else:
            raise SchemaValidationError(f"{path}: value matches a forbidden schema")
    if "if" in schema:
        try:
            _validate(value, schema["if"], root, path)
        except SchemaValidationError:
            conditional = schema.get("else")
        else:
            conditional = schema.get("then")
        if conditional is not None:
            _validate(value, conditional, root, path)
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise SchemaValidationError(f"{path}: value must equal {schema['const']!r}")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise SchemaValidationError(f"{path}: value is outside the allowed enum")
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        raise SchemaValidationError(f"{path}: expected {expected_type}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = sorted(item for item in required if item not in value)
        if missing:
            raise SchemaValidationError(f"{path}: missing required fields: {', '.join(missing)}")
        if len(value) < schema.get("minProperties", 0):
            raise SchemaValidationError(f"{path}: object has too few properties")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                _validate(item, properties[key], root, child_path)
            elif additional is False:
                raise SchemaValidationError(f"{child_path}: additional property is not allowed")
            elif isinstance(additional, dict):
                _validate(item, additional, root, child_path)

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise SchemaValidationError(f"{path}: array has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(f"{path}: array has too many items")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(canonical) != len(set(canonical)):
                raise SchemaValidationError(f"{path}: array items must be unique")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                _validate(item, schema["items"], root, f"{path}[{index}]")
        if "contains" in schema:
            for item in value:
                try:
                    _validate(item, schema["contains"], root, path)
                except SchemaValidationError:
                    continue
                break
            else:
                raise SchemaValidationError(f"{path}: array has no required matching item")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise SchemaValidationError(f"{path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaValidationError(f"{path}: string does not match the required pattern")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SchemaValidationError(f"{path}: invalid date-time") from exc
            if "T" not in value.upper() or parsed.tzinfo is None:
                raise SchemaValidationError(f"{path}: date-time must include time and timezone")
        elif "format" in schema:
            raise SchemaValidationError(f"{path}: unsupported format {schema['format']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path}: value is below the minimum")


def validate_json_schema(value: Any, schema: dict[str, Any]) -> None:
    """Validate an instance and reject schema keywords this offline validator cannot enforce."""
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema root must be an object")
    _validate(value, schema, schema, "$")
