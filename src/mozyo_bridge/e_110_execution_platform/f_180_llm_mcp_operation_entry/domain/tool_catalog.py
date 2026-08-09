"""The closed tool vocabulary and its typed schemas (pure, Redmine #15161).

Four read/plan tools, and no way to express a fifth from outside this module. The
catalog is a frozen table, :func:`tool_definition` fails closed on an unknown name,
and there is no registration hook — a new tool is a source change that a reviewer
sees, never a runtime extension. That is also why no external plugin API is
exposed: this Feature's non-goals inherit
``plugin-ready-adapter-boundary.md``'s.

The negative surface is enforced, not merely intended. #15148's Boundary forbids
publishing arbitrary command strings, shell argv, raw pane / tmux operations, and
mutating handoff / sublane operations as tools. A prose promise is not a
guarantee, so :func:`catalog_surface_violations` walks every declared schema and
reports any property whose name or enum value lands in
:data:`FORBIDDEN_PROPERTY_TOKENS`. The server calls it at startup and refuses to
serve a violating catalog; a test calls the same function, so the invariant cannot
rot into a comment.

Argument validation is a **closed subset** of JSON Schema — the constructs these
four schemas actually use (``type`` / ``properties`` / ``required`` /
``additionalProperties`` / ``items`` / ``enum`` / ``minItems`` / ``minLength``).
Implementing that subset rather than adding a ``jsonschema`` dependency keeps the
published package's dependency set as small as it is today, and the subset is
small enough to be exhaustively tested. :func:`validate_arguments` is fail-closed:
an unsupported keyword in a schema is a *catalog* error surfaced by
:func:`catalog_surface_violations`, never a silently skipped check at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E501
    REQUIRED_SELECTOR_FIELDS,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E501
    AXES,
)

# --- tool names (closed) --------------------------------------------------- #

TOOL_DOCS_RESOLVE = "docs_resolve"
TOOL_WORKFLOW_GLANCE = "workflow_glance"
TOOL_WORKFLOW_STEP_PLAN = "workflow_step_plan"
TOOL_UNIT_STATE = "unit_state"

TOOL_NAMES = (
    TOOL_DOCS_RESOLVE,
    TOOL_WORKFLOW_GLANCE,
    TOOL_WORKFLOW_STEP_PLAN,
    TOOL_UNIT_STATE,
)

#: Property-name and enum-value substrings that would publish a forbidden
#: capability. Matched as substrings on a lowercased token, so ``run_command`` and
#: ``tmux_pane`` are both caught. This is the machine-checkable form of #15148's
#: Boundary; :func:`catalog_surface_violations` is what makes it binding.
FORBIDDEN_PROPERTY_TOKENS = frozenset(
    {
        "argv",
        "command",
        "cmd",
        "eval",
        "exec",
        "keys",
        "keystroke",
        "pane",
        "script",
        "send",
        "shell",
        "spawn",
        "subprocess",
        "tmux",
    }
)

#: JSON Schema keywords :func:`validate_arguments` understands. A schema using
#: anything else is a catalog defect, because an unimplemented keyword would mean
#: a declared constraint that is never actually checked.
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "default",
    }
)

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, (list, tuple)) and not isinstance(v, (str, bytes)),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


@dataclass(frozen=True)
class ToolDefinition:
    """One published tool.

    ``read_only`` is stated on every tool in this catalog and is not a hint the
    handler may contradict: the handlers reach read-only application processing,
    and the mutating operations live behind #15152's separate authority work.
    """

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    read_only: bool = True

    def as_payload(self) -> dict:
        """The MCP ``tools/list`` entry for this tool."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": _plain(self.input_schema),
            "outputSchema": _plain(self.output_schema),
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }


def _plain(value: Any) -> Any:
    """Deep-copy a frozen schema into plain JSON containers for serialization."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    """Deep-freeze a schema literal so a consumer cannot mutate the catalog."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


# --- shared schema fragments ----------------------------------------------- #

_UNIT_SELECTOR_SCHEMA = {
    "type": "object",
    "description": (
        "Exact UnitRecord identity. workspace_id + lane_id + project_id are "
        "required; host_id / repo_label / ticket_system only narrow. A selector "
        "matching more than one Unit is refused as ambiguous rather than guessed."
    ),
    "properties": {
        "workspace_id": {
            "type": "string",
            "minLength": 1,
            "description": "The Unit's workspace identity.",
        },
        "lane_id": {
            "type": "string",
            "minLength": 1,
            "description": "The Unit's lane identity (`default` for the main lane).",
        },
        "project_id": {
            "type": "string",
            "minLength": 1,
            "description": "The project / governance context governing the Unit.",
        },
        "host_id": {"type": "string", "description": "Narrowing: host identity."},
        "repo_label": {"type": "string", "description": "Narrowing: repo label."},
        "ticket_system": {
            "type": "string",
            "description": "Narrowing: governing ticket system.",
        },
    },
    "required": list(REQUIRED_SELECTOR_FIELDS),
    "additionalProperties": False,
}

_SOURCE_HEALTH_SCHEMA = {
    "type": "object",
    "description": "Which sources were readable, so an empty result is never read as 'nothing active'.",
    "properties": {
        "degraded": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["degraded", "notes"],
}


# --- the four tools -------------------------------------------------------- #

_DOCS_RESOLVE = ToolDefinition(
    name=TOOL_DOCS_RESOLVE,
    title="Resolve governing docs for paths",
    description=(
        "Resolve, through the repo's docs catalog, which guardrail / spec / "
        "convention documents govern the given repo-relative paths. Read-only. "
        "Equivalent to `mozyo-bridge docs resolve`, calling the same catalog "
        "resolver in-process."
    ),
    input_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Repo-relative paths to resolve documents for. Must stay "
                        "inside the repo: an absolute path, or one that escapes "
                        "the repo root via `..`, is refused."
                    ),
                },
                "include_local": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Merge the git-ignored local catalog overlay. False gives "
                        "the public-only view CI would see."
                    ),
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        }
    ),
    output_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "resolutions": {"type": "array", "items": {"type": "object"}},
                "overlay_applied": {"type": "boolean"},
            },
            "required": ["resolutions", "overlay_applied"],
        }
    ),
)

_WORKFLOW_GLANCE = ToolDefinition(
    name=TOOL_WORKFLOW_GLANCE,
    title="Project active lanes onto workflow state",
    description=(
        "Project every active lane / UserStory onto its durable workflow state, "
        "next action and owner, and delivery anomaly. Read-only: it mutates "
        "nothing, sends nothing, and writes no Redmine record. A lane whose "
        "source was unreadable is reported as an explicit unknown, never dropped."
    ),
    input_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Restrict the projection to these issue ids. Omit to use "
                        "the active-lane roster for this repo's workspace."
                    ),
                },
            },
            "additionalProperties": False,
        }
    ),
    output_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "closed_coordinator_debt": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "source_health": _SOURCE_HEALTH_SCHEMA,
            },
            "required": ["rows", "source_health"],
        }
    ),
)

_WORKFLOW_STEP_PLAN = ToolDefinition(
    name=TOOL_WORKFLOW_STEP_PLAN,
    title="Resolve the next safe workflow step (plan only)",
    description=(
        "Resolve which single workflow step would be safe next for the current "
        "lane, and report it. PLAN ONLY: this tool never dispatches the resolved "
        "step, never delivers a handoff, and never writes a durable record — it "
        "is the resolution half of `mozyo-bridge workflow step`, run through the "
        "same pure state machine. Executing a step is not exposed here."
    ),
    input_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "Durable anchor issue id for the step, when one applies.",
                },
                "journal": {
                    "type": "string",
                    "description": "Durable anchor journal id for the step, when one applies.",
                },
            },
            "additionalProperties": False,
        }
    ),
    output_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "plan": {"type": "object"},
                "execution": {"type": "string"},
                "source_health": _SOURCE_HEALTH_SCHEMA,
            },
            "required": ["plan", "execution", "source_health"],
        }
    ),
)

_UNIT_STATE = ToolDefinition(
    name=TOOL_UNIT_STATE,
    title="Read one Unit's state on four independent axes",
    description=(
        "Read the state of one explicitly named Unit. Returns four INDEPENDENT "
        "axes — workflow (durable Redmine record), runtime (observed terminal "
        "runtime), delivery (dispatch outcome), health (anomaly / degraded / "
        "freshness) — each field carrying source, observed_at and freshness. "
        "Unobservable values stay `unknown` / `unconfirmed`: a journal that has "
        "not moved, a silent stdout, or an ended turn is never reported as "
        "blocked, idle, or completed. `blocked` appears only with an "
        "authoritative blocker source, a reason, and a resume condition. "
        "Read-only; returns no routing target and no permission to act."
    ),
    input_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "unit": _UNIT_SELECTOR_SCHEMA,
                "axes": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(AXES)},
                    "description": "Restrict the report to these axes. Omit for all four.",
                },
            },
            "required": ["unit"],
            "additionalProperties": False,
        }
    ),
    output_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "unit": {"type": "object"},
                "workflow": {"type": "object"},
                "runtime": {"type": "object"},
                "delivery": {"type": "object"},
                "health": {"type": "object"},
                "read_only": {"type": "boolean"},
            },
            "required": ["unit", "read_only"],
        }
    ),
)

#: The published catalog, in ``tools/list`` order.
TOOL_CATALOG: Mapping[str, ToolDefinition] = MappingProxyType(
    {
        definition.name: definition
        for definition in (
            _DOCS_RESOLVE,
            _WORKFLOW_GLANCE,
            _WORKFLOW_STEP_PLAN,
            _UNIT_STATE,
        )
    }
)


class UnknownToolError(LookupError):
    """The requested tool is not in the closed catalog."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool: {name!r}")
        self.name = name


def tool_definition(name: str) -> ToolDefinition:
    """The definition for ``name``, or :class:`UnknownToolError` (fail-closed)."""
    try:
        return TOOL_CATALOG[name]
    except KeyError as exc:
        raise UnknownToolError(name) from exc


def list_tools_payload() -> list:
    """The ``tools/list`` ``tools`` array."""
    return [definition.as_payload() for definition in TOOL_CATALOG.values()]


# --- surface guard --------------------------------------------------------- #


def _walk_schema(schema: Any, path: str):
    """Yield ``(path, schema)`` for every subschema, depth first."""
    if isinstance(schema, Mapping):
        yield path, schema
        for key, value in schema.items():
            if key == "properties" and isinstance(value, Mapping):
                for name, sub in value.items():
                    yield from _walk_schema(sub, f"{path}.{name}")
            elif key == "items":
                yield from _walk_schema(value, f"{path}[]")


def _forbidden_token_in(text: str) -> str:
    lowered = text.lower()
    for token in sorted(FORBIDDEN_PROPERTY_TOKENS):
        if token in lowered:
            return token
    return ""


def catalog_surface_violations(
    catalog: Mapping[str, ToolDefinition] = TOOL_CATALOG,
) -> tuple[str, ...]:
    """Report every way ``catalog`` would publish a forbidden capability.

    Two checks, both structural:

    - no **input** property name (at any depth) and no enum value contains a
      :data:`FORBIDDEN_PROPERTY_TOKENS` token — so no tool can accept an arbitrary
      command string, shell argv, or a raw pane / tmux target;
    - every input schema uses only :data:`SUPPORTED_SCHEMA_KEYWORDS` — so no
      declared constraint goes unchecked at call time.

    Output schemas are exempt from the token check: reporting *that* a delivery
    anomaly exists is the read model's job, and refusing the word there would
    forbid describing the very state this Feature exists to surface. Only the
    input side can be used to ask for a side effect.
    """
    violations: list[str] = []
    for name, definition in catalog.items():
        if not definition.read_only:
            violations.append(f"{name}: catalog publishes a non-read-only tool")
        for path, schema in _walk_schema(definition.input_schema, name):
            unsupported = set(schema.keys()) - SUPPORTED_SCHEMA_KEYWORDS
            if unsupported:
                violations.append(
                    f"{path}: unsupported schema keyword(s) "
                    f"{', '.join(sorted(unsupported))}; validate_arguments would "
                    "not enforce them"
                )
            properties = schema.get("properties")
            if isinstance(properties, Mapping):
                for prop in properties:
                    token = _forbidden_token_in(str(prop))
                    if token:
                        violations.append(
                            f"{path}.{prop}: input property name publishes the "
                            f"forbidden capability token {token!r}"
                        )
            enum = schema.get("enum")
            if isinstance(enum, (list, tuple)):
                for value in enum:
                    token = _forbidden_token_in(str(value))
                    if token:
                        violations.append(
                            f"{path}: enum value {value!r} publishes the forbidden "
                            f"capability token {token!r}"
                        )
    return tuple(violations)


# --- argument validation (closed JSON Schema subset) ----------------------- #


def _validate(value: Any, schema: Mapping[str, Any], path: str, errors: list) -> None:
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
                _validate(value[name], sub, f"{path}.{name}", errors)

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
                _validate(item, items, f"{path}[{index}]", errors)


def validate_arguments(
    definition: ToolDefinition, arguments: Mapping[str, Any]
) -> tuple[str, ...]:
    """Validate ``arguments`` against ``definition``'s input schema.

    Returns **every** violation rather than the first, so a caller repairs the
    call in one round. An empty tuple means the arguments satisfy the declared
    subset; the handler still applies its own domain refusals (an argument can be
    schema-valid and still name a Unit that does not exist).
    """
    errors: list[str] = []
    _validate(arguments, definition.input_schema, "arguments", errors)
    return tuple(errors)


def default_arguments(definition: ToolDefinition) -> dict:
    """Top-level schema defaults, so a handler reads one resolved argument set."""
    properties = definition.input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return {
        name: sub["default"]
        for name, sub in properties.items()
        if isinstance(sub, Mapping) and "default" in sub
    }


def resolve_arguments(
    definition: ToolDefinition, arguments: Mapping[str, Any]
) -> dict:
    """``arguments`` merged over the schema defaults."""
    resolved = default_arguments(definition)
    resolved.update(dict(arguments))
    return resolved


__all__ = (
    "FORBIDDEN_PROPERTY_TOKENS",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "TOOL_CATALOG",
    "TOOL_DOCS_RESOLVE",
    "TOOL_NAMES",
    "TOOL_UNIT_STATE",
    "TOOL_WORKFLOW_GLANCE",
    "TOOL_WORKFLOW_STEP_PLAN",
    "ToolDefinition",
    "UnknownToolError",
    "catalog_surface_violations",
    "default_arguments",
    "list_tools_payload",
    "resolve_arguments",
    "tool_definition",
    "validate_arguments",
)
