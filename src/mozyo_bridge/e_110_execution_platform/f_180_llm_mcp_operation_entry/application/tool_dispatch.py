"""Typed tool dispatch (Redmine #15161).

One table from the closed tool vocabulary to its handler, and one place where the
two MCP error channels are separated:

- a **protocol error** (unknown tool, arguments that violate the declared input
  schema) is a JSON-RPC error, because the call was never valid;
- a **tool execution error** (a source that could not be read, a Unit selector the
  caller must fix) rides in the result with ``isError: true``, because the call was
  valid and the answer is a refusal.

Getting that split wrong is not cosmetic: a caller that receives a JSON-RPC error
for "Redmine was unreachable" cannot see the structured reason, and a caller that
receives ``isError`` for "you sent an unknown tool" will retry the same broken
call. :func:`dispatch_tool_call` returns a typed
:class:`~...domain.jsonrpc`-agnostic result so the server renders each on its own
channel.

Every result carries the structured payload **and** a serialized JSON text block.
The text block is the MCP backwards-compatibility requirement for structured
content; the ``structuredContent`` object is what a caller should read. Neither is
prose to be parsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
    ReadPlanContext,
    ToolOutcome,
    run_docs_resolve,
    run_workflow_glance,
    run_workflow_step_plan,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E501
    run_handoff_reply,
    run_handoff_send,
    run_sublane_start_tool,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E501
    run_unit_state,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
    TOOL_DOCS_RESOLVE,
    TOOL_HANDOFF_REPLY,
    TOOL_HANDOFF_SEND,
    TOOL_NAMES,
    TOOL_SUBLANE_START,
    TOOL_UNIT_STATE,
    TOOL_WORKFLOW_GLANCE,
    TOOL_WORKFLOW_STEP_PLAN,
    ToolDefinition,
    UnknownToolError,
    conforming_skeleton,
    resolve_arguments,
    tool_definition,
    validate_arguments,
    validate_output,
)

#: name -> handler. Statically bound built-in functions only; there is no
#: registration hook and no path-resolved import, so dispatch can never reach code
#: outside this package. The mutating handlers (#15152) reach the typed shared
#: application services — never a subprocess, never a raw pane.
_HANDLERS: Mapping[str, Callable[[Mapping[str, Any], ReadPlanContext], ToolOutcome]] = {
    TOOL_DOCS_RESOLVE: run_docs_resolve,
    TOOL_WORKFLOW_GLANCE: run_workflow_glance,
    TOOL_WORKFLOW_STEP_PLAN: run_workflow_step_plan,
    TOOL_UNIT_STATE: run_unit_state,
    TOOL_HANDOFF_SEND: run_handoff_send,
    TOOL_HANDOFF_REPLY: run_handoff_reply,
    TOOL_SUBLANE_START: run_sublane_start_tool,
}


@dataclass(frozen=True)
class ProtocolError:
    """A call that was never valid. The server renders it as a JSON-RPC error."""

    message: str
    data: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class DispatchResult:
    """The outcome of one ``tools/call``.

    Exactly one of ``result`` / ``protocol_error`` is set.
    """

    result: Optional[Mapping[str, Any]] = None
    protocol_error: Optional[ProtocolError] = None

    @property
    def is_protocol_error(self) -> bool:
        return self.protocol_error is not None


def _tool_result(outcome: ToolOutcome, definition: ToolDefinition) -> dict:
    """Render a handler outcome as an MCP tool result — schema-conformant or withheld.

    Review j#103251 r4f4: a declared ``outputSchema`` is a promise about
    ``structuredContent``, and every error path here was breaking it — a selector
    refusal or a generic failure carried none of the schema's required members.
    Two-step, both fail-closed:

    1. An error payload missing required members is projected into the declared
       shape (``conforming_skeleton`` under it, the typed error fields on top), so
       a typed refusal stays readable AND conformant.
    2. Anything still nonconforming — including a SUCCESS payload, which would
       mean this server's own handler broke its schema — is **withheld**: the
       result carries no ``structuredContent`` at all and reports the mismatch as
       a tool error naming schema paths only. A nonconforming structured object
       misleads a schema-trusting client; no structured object merely degrades it.
    """
    payload = dict(outcome.payload)
    violations = validate_output(definition, payload)
    if violations and outcome.is_error:
        skeleton = conforming_skeleton(definition.output_schema)
        payload = {**(skeleton if isinstance(skeleton, dict) else {}), **payload}
        violations = validate_output(definition, payload)
    if violations:
        mismatch = {
            "error": "output_schema_mismatch",
            "tool": definition.name,
            "violations": list(violations),
        }
        body = json.dumps(mismatch, ensure_ascii=False, sort_keys=True)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "the tool result did not conform to the declared output "
                        f"schema and was withheld\n{body}"
                    ),
                }
            ],
            "isError": True,
        }
    text = outcome.summary or ""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    content = [{"type": "text", "text": f"{text}\n{body}" if text else body}]
    return {"content": content, "structuredContent": payload, "isError": outcome.is_error}


def dispatch_tool_call(
    name: str,
    arguments: Mapping[str, Any],
    context: ReadPlanContext,
) -> DispatchResult:
    """Validate and run one tool call.

    An unexpected exception from a handler becomes a tool execution error rather
    than propagating: a long-lived stdio server must answer every request it
    accepted, and a traceback escaping into the transport would leave the caller
    waiting on a response that never arrives. The exception *type* is reported;
    the message is not, because a source's exception text can carry a path or a
    credential and this surface must not emit either.
    """
    try:
        definition = tool_definition(name)
    except UnknownToolError:
        return DispatchResult(
            protocol_error=ProtocolError(
                message=f"unknown tool: {name!r}",
                data={"available_tools": list(TOOL_NAMES)},
            )
        )

    errors = validate_arguments(definition, arguments)
    if errors:
        return DispatchResult(
            protocol_error=ProtocolError(
                message=f"invalid arguments for tool {definition.name!r}",
                data={"violations": list(errors)},
            )
        )

    handler = _HANDLERS[definition.name]
    resolved = resolve_arguments(definition, arguments)
    try:
        outcome = handler(resolved, context)
    except Exception as exc:  # noqa: BLE001 - see docstring: never escape the transport
        outcome = ToolOutcome(
            payload={
                "error": "tool_failed",
                "tool": definition.name,
                "exception": type(exc).__name__,
            },
            is_error=True,
            summary=f"{definition.name} failed with {type(exc).__name__}",
        )
    return DispatchResult(result=_tool_result(outcome, definition))


__all__ = ("DispatchResult", "ProtocolError", "dispatch_tool_call")
