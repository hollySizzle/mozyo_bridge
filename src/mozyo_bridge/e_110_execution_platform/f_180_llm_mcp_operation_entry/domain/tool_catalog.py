"""The closed tool vocabulary and its typed schemas (pure, Redmine #15161 / #15152).

Four read/plan tools plus three declared mutating tools, and no way to express an
eighth from outside this module. The catalog is a frozen table,
:func:`tool_definition` fails closed on an unknown name, and there is no
registration hook — a new tool is a source change that a reviewer sees, never a
runtime extension. That is also why no external plugin API is exposed: this
Feature's non-goals inherit ``plugin-ready-adapter-boundary.md``'s.

The negative surface is enforced, not merely intended. #15148's Boundary forbids
publishing arbitrary command strings, shell argv, and raw pane / tmux operations
as tools; #15152 adds the HIGH-LEVEL mutating handoff / sublane operations while
keeping that boundary intact. A prose promise is not a guarantee, so
:func:`catalog_surface_violations` walks every declared schema — mutating tools
included — and reports any property whose name or enum value lands in
:data:`FORBIDDEN_PROPERTY_TOKENS`, plus any non-read-only tool that is not a
member of the closed :data:`MUTATING_TOOL_NAMES` declaration (and any declared
mutating name that claims to be read-only). The server calls it at startup and
refuses to serve a violating catalog; a test calls the same function, so the
invariant cannot rot into a comment.

The mutating tools carry no authority of their own: each handler calls the same
shared application processing the CLI calls (#15149's ``run_handoff``, #15152's
``run_sublane_start``), so authority / identity / anchor / send-safety gates run
identically for both entries and refuse with typed reasons BEFORE any side
effect. A receiver is named by ROLE and a lane by its identity — a pane locator,
tmux target, or command string is not representable in these schemas, which is
how the surface refuses to address the unmanaged (receipt-less) rows measured in
#15152 j#102930 / j#102998.

Argument validation is a **closed subset** of JSON Schema — the constructs these
schemas actually use (``type`` / ``properties`` / ``required`` /
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

from mozyo_bridge.core.state.lane_kind import LANE_KINDS
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    KIND_LABELS,
    SOURCES,
)
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
TOOL_HANDOFF_SEND = "handoff_send"
TOOL_HANDOFF_REPLY = "handoff_reply"
TOOL_SUBLANE_START = "sublane_start"

TOOL_NAMES = (
    TOOL_DOCS_RESOLVE,
    TOOL_WORKFLOW_GLANCE,
    TOOL_WORKFLOW_STEP_PLAN,
    TOOL_UNIT_STATE,
    TOOL_HANDOFF_SEND,
    TOOL_HANDOFF_REPLY,
    TOOL_SUBLANE_START,
)

#: The CLOSED declaration of which tools are allowed to be non-read-only
#: (Redmine #15152). :func:`catalog_surface_violations` enforces it both ways: a
#: non-read-only tool outside this set is a violation, and a member claiming
#: ``read_only`` is one too — the annotation a client plans around must match the
#: declaration a reviewer sees.
MUTATING_TOOL_NAMES = frozenset(
    {TOOL_HANDOFF_SEND, TOOL_HANDOFF_REPLY, TOOL_SUBLANE_START}
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

# The recursive validator, the freeze/copy helpers, and the skeleton projection
# live in `.tool_schema_subset` (mechanical #15152 carve-out for module health).
# `conforming_skeleton` is re-exported here so import sites are unchanged.
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_schema_subset import (  # noqa: E501
    conforming_skeleton,
    freeze as _freeze,
    plain as _plain,
    validate_value,
)


@dataclass(frozen=True)
class ToolDefinition:
    """One published tool.

    ``read_only`` is stated on every tool in this catalog and is not a hint the
    handler may contradict: read-only handlers reach read-only application
    processing, and a ``read_only=False`` tool is only publishable when its name
    is a member of the closed :data:`MUTATING_TOOL_NAMES` declaration (#15152) —
    :func:`catalog_surface_violations` refuses everything else at startup.
    """

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    read_only: bool = True

    def as_payload(self) -> dict:
        """The MCP ``tools/list`` entry for this tool.

        A mutating tool is honestly annotated: not read-only and not idempotent
        (re-running a delivered send or an executed lane creation is a second
        operation, not a no-op). ``destructiveHint`` stays False for every tool —
        the published mutating surface is additive-only (a governed send / an
        additive lane create); no tool here deletes, kills, or overwrites.
        """
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": _plain(self.input_schema),
            "outputSchema": _plain(self.output_schema),
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": False,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


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
    # What `conforming_skeleton` fills on an error result. The boolean zero would
    # claim "sources healthy" on a call that just FAILED — the exact structured
    # misdirection r4f4 exists to prevent — so the neutral here is the axis's own
    # fail-closed resting value, mirroring `HealthAxis.degraded = True`.
    "default": {"degraded": True, "notes": []},
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
                # The default is the tool's fixed EXECUTION_PLAN_ONLY token, so a
                # consumer asserting "this surface never executed" reads the same
                # answer on an error result as on a plan; the string zero would
                # break that contract. Pinned against the application constant by
                # regression, since the domain layer cannot import it.
                "execution": {"type": "string", "default": "plan_only"},
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
                # `default` is what `conforming_skeleton` fills on an error result
                # (review j#103251 r4f4): even a refusal comes from a read-only
                # tool, so the neutral value here is True, not the bool zero.
                "read_only": {"type": "boolean", "default": True},
            },
            "required": ["unit", "read_only"],
        }
    ),
)

# --- the three mutating tools (Redmine #15152) ----------------------------- #
#
# Each is the typed MCP entry over the SAME shared application processing the
# CLI runs — no judgement is restated here, and none can be skipped. The schemas
# deliberately cannot name a pane, a tmux target, or a command string: a
# receiver is a ROLE, a lane is an identity, and an anchor is issue/journal
# (or task/comment) ids. Authority, identity, anchor-ownership, route, and
# send-safety gates all run inside the shared orchestration and refuse with
# typed reasons before any side effect.

_HANDOFF_ANCHOR_PROPERTIES = {
    "source": {
        "type": "string",
        "enum": sorted(SOURCES),
        "default": "redmine",
        "description": (
            "Durable anchor system. `redmine` anchors with issue+journal; "
            "`asana` anchors with task_id+comment_id. Cross-source fields are "
            "refused by the shared anchor gate."
        ),
    },
    "issue": {
        "type": "string",
        "description": "Redmine durable-anchor issue id (required by the gate for source=redmine).",
    },
    "journal": {
        "type": "string",
        "description": "Redmine durable-anchor journal id (required by the gate for source=redmine).",
    },
    "task_id": {
        "type": "string",
        "description": "Asana durable-anchor task id (source=asana).",
    },
    "comment_id": {
        "type": "string",
        "description": "Asana durable-anchor comment id (source=asana).",
    },
}


def _handoff_input_schema(operation_note: str) -> Mapping[str, Any]:
    return _freeze(
        {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Receiver ROLE vocabulary (e.g. `claude` / `codex`), "
                        "validated by the orchestration's receiver gate. Never "
                        "a pane locator: delivery resolves only through managed "
                        "assigned identity, so an unmanaged (identity-less) row "
                        "cannot be addressed."
                    ),
                },
                **_HANDOFF_ANCHOR_PROPERTIES,
                "kind": {
                    "type": "string",
                    "enum": sorted(KIND_LABELS),
                    "description": (
                        "Handoff intent label. " + operation_note
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Optional summary line (required by the gate for kind=custom).",
                },
                "lane": {
                    "type": "string",
                    "description": (
                        "Target lane identity (narrowing). The lane's receiver "
                        "is resolved from durable lane authority, not from a "
                        "pane position."
                    ),
                },
                "target_repo": {
                    "type": "string",
                    "default": "auto",
                    "description": "Target-repo resolution (default: auto).",
                },
            },
            "required": ["to"],
            "additionalProperties": False,
        }
    )


#: Output schema shared by the two handoff tools. `default`s are the fail-closed
#: neutrals `conforming_skeleton` fills on an error result: a failed call must
#: never look delivered.
_HANDOFF_OUTPUT_SCHEMA = _freeze(
    {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "default": ""},
            "status": {"type": "string", "default": "fail_closed"},
            "exit_code": {"type": "integer", "default": 2},
            "delivered": {"type": "boolean", "default": False},
            "injection_stage": {"type": "string", "default": ""},
            "outcome": {"type": "object", "default": {}},
            "refusal": {"type": "string", "default": ""},
        },
        "required": ["operation", "status", "delivered", "outcome"],
    }
)

_HANDOFF_MUTATION_DESCRIPTION = (
    "MUTATING. Runs the same shared handoff orchestration the CLI runs "
    "(in-process, #15149): durable-anchor ownership, receiver vocabulary, "
    "identity, gateway-route and send-safety gates all apply and refuse with "
    "typed reasons before any side effect. Delivery is reported from the shared "
    "injection-stage authority; `delivered: false` with status `completed` "
    "means the send terminated without confirmed submission — never assume "
    "delivery from exit alone."
)

_HANDOFF_SEND = ToolDefinition(
    name=TOOL_HANDOFF_SEND,
    title="Send an anchored cross-agent handoff",
    description=(
        "Send a governed, durable-anchored handoff to a receiver ROLE. "
        + _HANDOFF_MUTATION_DESCRIPTION
    ),
    input_schema=_handoff_input_schema(
        "Defaults per the shared entry policy for `send`."
    ),
    output_schema=_HANDOFF_OUTPUT_SCHEMA,
    read_only=False,
)

_HANDOFF_REPLY = ToolDefinition(
    name=TOOL_HANDOFF_REPLY,
    title="Reply on an anchored handoff rail",
    description=(
        "Reply to a received handoff on the anchored reply rail (`kind` "
        "defaults to `reply` per the shared entry policy). "
        + _HANDOFF_MUTATION_DESCRIPTION
    ),
    input_schema=_handoff_input_schema("Defaults to `reply` for this operation."),
    output_schema=_HANDOFF_OUTPUT_SCHEMA,
    read_only=False,
)

_SUBLANE_START = ToolDefinition(
    name=TOOL_SUBLANE_START,
    title="Plan or actuate a sublane (worktree + managed pair + dispatch)",
    description=(
        "MUTATING when `actuate` is true; the default is the side-effect-free "
        "actuation plan. Runs the same typed shared service the CLI's `sublane "
        "create/start` runs: the work-unit granularity gate, the #15146 "
        "delegated_coordinator PARENT-AUTHORITY admission (a delegated_"
        "coordinator lane is refused with a typed verdict unless its parent "
        "project gateway is durably declared AND verified), the provider "
        "launchability preflight, and every actuation gate (identity, anchor, "
        "sender attestation, fill admission) — all decided BEFORE any worktree "
        "/ pair / dispatch side effect. Launching goes only through the "
        "managed creator rail that assigns durable identity; no raw pane "
        "creation is expressible."
    ),
    input_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Ticket issue id the lane implements.",
                },
                "lane_label": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Lane label (e.g. issue_<id>_<slug>).",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch for the lane worktree (required in a Git workspace).",
                },
                "worktree": {
                    "type": "string",
                    "description": "Worktree path for the lane (required in a Git workspace).",
                },
                "journal": {
                    "type": "string",
                    "description": "Durable-anchor journal id for the dispatch step.",
                },
                "lane_kind": {
                    "type": "string",
                    "enum": sorted(LANE_KINDS),
                    "description": (
                        "Delegation-geometry kind. `delegated_coordinator` "
                        "asserts a parent project gateway and is admitted only "
                        "when that parent authority is durably declared and "
                        "verified (#15146)."
                    ),
                },
                "work_unit": {
                    "type": "string",
                    "enum": ["epic", "feature", "user_story", "leaf_issue"],
                    "description": "Dispatched work-unit granularity (default: repo-local config, else user_story).",
                },
                "work_unit_decision_journal": {
                    "type": "string",
                    "description": "Durable journal id authorizing an oversized / leaf-with-parent work unit.",
                },
                "leaf_standalone": {
                    "type": "boolean",
                    "default": False,
                    "description": "Declare the leaf_issue has no parent UserStory.",
                },
                "base_ref": {
                    "type": "string",
                    "description": "Explicit git base ref the lane worktree branches from.",
                },
                "actuate": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "true actuates (worktree + managed pair + governed "
                        "dispatch) — the CLI's --execute; false (default) "
                        "resolves the same plan with zero side effects. Named "
                        "`actuate` because this surface's guard forbids any "
                        "input property carrying an exec-like token."
                    ),
                },
                "dispatch": {
                    "type": "boolean",
                    "default": True,
                    "description": "With actuate, also dispatch the governed implementation_request.",
                },
                "target_repo": {
                    "type": "string",
                    "default": "auto",
                    "description": "Target-repo resolution for the dispatch (default: auto).",
                },
            },
            "required": ["issue", "lane_label"],
            "additionalProperties": False,
        }
    ),
    output_schema=_freeze(
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "refused"},
                "executed": {"type": "boolean", "default": False},
                "exit_code": {"type": "integer", "default": 1},
                "refusal_reason": {"type": "string", "default": ""},
                "refusal": {"type": "string", "default": ""},
                "outcome": {"type": "object", "default": {}},
            },
            "required": ["status", "executed", "outcome"],
        }
    ),
    read_only=False,
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
            _HANDOFF_SEND,
            _HANDOFF_REPLY,
            _SUBLANE_START,
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
    mutating_names: frozenset = MUTATING_TOOL_NAMES,
) -> tuple[str, ...]:
    """Report every way ``catalog`` would publish a forbidden capability.

    Structural checks:

    - a non-read-only tool must be a member of the closed ``mutating_names``
      declaration (#15152), and a declared mutating name must not claim
      ``read_only`` — the annotation a client plans around must match the
      reviewed declaration, in both directions;
    - no **input** property name (at any depth) and no enum value contains a
      :data:`FORBIDDEN_PROPERTY_TOKENS` token — so no tool, mutating tools
      included, can accept an arbitrary command string, shell argv, or a raw
      pane / tmux target;
    - every input schema uses only :data:`SUPPORTED_SCHEMA_KEYWORDS` — so no
      declared constraint goes unchecked at call time;
    - every OUTPUT schema uses only supported keywords too (added with
      :func:`validate_output`): since r4f4 the output schema is enforced before
      send, so an unimplemented keyword there would be a declared constraint
      ``_validate`` silently skips — the exact defect the input rule prevents.

    Output schemas are exempt from the token check: reporting *that* a delivery
    anomaly exists is the read model's job, and refusing the word there would
    forbid describing the very state this Feature exists to surface. Only the
    input side can be used to ask for a side effect.
    """
    violations: list[str] = []
    for name, definition in catalog.items():
        if not definition.read_only and name not in mutating_names:
            violations.append(
                f"{name}: catalog publishes an undeclared mutating "
                "(non-read-only) tool"
            )
        if definition.read_only and name in mutating_names:
            violations.append(
                f"{name}: a declared mutating tool claims to be read-only"
            )
        for path, schema in _walk_schema(definition.output_schema, f"{name}(output)"):
            unsupported = set(schema.keys()) - SUPPORTED_SCHEMA_KEYWORDS
            if unsupported:
                violations.append(
                    f"{path}: unsupported schema keyword(s) "
                    f"{', '.join(sorted(unsupported))}; validate_output would "
                    "not enforce them"
                )
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
# The recursive validator / skeleton live in `.tool_schema_subset` (mechanical
# #15152 carve-out for module health); these wrappers keep the public surface.


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
    validate_value(arguments, definition.input_schema, "arguments", errors)
    return tuple(errors)


def validate_output(
    definition: ToolDefinition, payload: Mapping[str, Any]
) -> tuple[str, ...]:
    """Validate a would-be ``structuredContent`` against the declared output schema.

    Review j#103251 r4f4: the MCP spec makes a declared ``outputSchema`` a promise
    about ``structuredContent``, and this server was breaking it on every error
    path — a selector refusal or a generic handler failure produced a payload with
    none of the schema's required members. Validation is the send-side half of the
    fix; :func:`conforming_skeleton` is the projection half. Violation strings name
    schema paths and type names only — never payload values.
    """
    errors: list[str] = []
    validate_value(payload, definition.output_schema, "structuredContent", errors)
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
    "MUTATING_TOOL_NAMES",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "TOOL_CATALOG",
    "TOOL_DOCS_RESOLVE",
    "TOOL_HANDOFF_REPLY",
    "TOOL_HANDOFF_SEND",
    "TOOL_NAMES",
    "TOOL_SUBLANE_START",
    "TOOL_UNIT_STATE",
    "TOOL_WORKFLOW_GLANCE",
    "TOOL_WORKFLOW_STEP_PLAN",
    "ToolDefinition",
    "UnknownToolError",
    "catalog_surface_violations",
    "conforming_skeleton",
    "default_arguments",
    "list_tools_payload",
    "resolve_arguments",
    "tool_definition",
    "validate_arguments",
    "validate_output",
)
