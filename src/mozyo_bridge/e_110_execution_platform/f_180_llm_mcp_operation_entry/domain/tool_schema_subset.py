"""The closed JSON Schema subset the tool catalog validates with (pure, #15161).

Carved out of :mod:`.tool_catalog` when #15152 grew the catalog past the
module-health comfort zone — a mechanical move, not a behavior change. The
catalog module keeps its public wrappers (``validate_arguments`` /
``validate_output`` / ``conforming_skeleton`` / ...) so every import site is
unchanged; this module owns the recursive validator, the schema freeze/copy
helpers, and the skeleton projection.

The subset is deliberately closed (see ``SUPPORTED_SCHEMA_KEYWORDS`` in the
catalog): an unimplemented keyword is a *catalog* error surfaced by
``catalog_surface_violations``, never a silently skipped check at call time.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, (list, tuple)) and not isinstance(v, (str, bytes)),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def plain(value: Any) -> Any:
    """Deep-copy a frozen schema into plain JSON containers for serialization."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def freeze(value: Any) -> Any:
    """Deep-freeze a schema literal so a consumer cannot mutate the catalog."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def validate_value(
    value: Any, schema: Mapping[str, Any], path: str, errors: list
) -> None:
    """Append every violation of ``schema`` by ``value`` to ``errors``."""
    expected = schema.get("type")
    if expected is not None:
        check = _TYPE_CHECKS.get(expected)
        if check is None:
            errors.append(f"{path}: schema declares unsupported type {expected!r}")
            return
        if not check(value):
            errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
            return

    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        errors.append(f"{path}: {value!r} is not one of {list(enum)}")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}: must be at least {minimum} character(s)")

    if isinstance(value, Mapping):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        for name in schema.get("required", ()) or ():
            if name not in value:
                errors.append(f"{path}.{name}: required property is missing")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name}: unknown property is not accepted")
        for name, sub in properties.items():
            if name in value:
                validate_value(value[name], sub, f"{path}.{name}", errors)

    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}: must have at least {minimum} item(s)")
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}: must have at most {maximum} item(s)")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                validate_value(item, items, f"{path}[{index}]", errors)


def conforming_skeleton(schema: Mapping[str, Any]) -> Any:
    """The minimal value satisfying ``schema``: required members filled, neutrally.

    Recursive, because a required object member can carry its own ``required``
    list (``source_health`` does). A declared ``default`` wins over the type's
    neutral zero, so a schema can state what "no answer" means for a member — the
    read-only flag's neutral is True, a failed call's ``source_health`` is
    degraded, and a failed mutating call's ``delivered`` is False — never a
    healthy- or delivered-looking zero. Used to project an error payload into
    the declared shape: ``{**conforming_skeleton(schema), **error_fields}``.
    """
    if not isinstance(schema, Mapping):
        return None
    if "default" in schema:
        return plain(schema["default"])
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        return {
            name: conforming_skeleton(properties.get(name, {}))
            for name in schema.get("required", ()) or ()
        }
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind == "integer":
        return 0
    return ""


__all__ = ("conforming_skeleton", "freeze", "plain", "validate_value")
