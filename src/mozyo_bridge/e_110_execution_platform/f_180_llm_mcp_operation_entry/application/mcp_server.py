"""The non-interactive local MCP server over stdio (Redmine #15161).

Startable from the installed package and stoppable without a signal: the loop
reads newline-delimited frames from stdin and returns when stdin reaches EOF —
the shutdown the stdio transport defines (the client closes the input stream). It
never prompts, never blocks on a TTY, and never reads a terminal, so it runs
identically under a client that spawned it and under a test that hands it two
pipes.

Three rules the transport depends on, enforced here rather than assumed:

- **stdout carries MCP frames only.** Every diagnostic goes to ``stderr``. The
  server is constructed with explicit streams, so nothing in this module reaches
  for the process-global ``print``.
- **a frame is never partially written.** Each response is rendered whole by
  ``encode_message`` (which refuses a frame containing a newline) and then written
  and flushed as one line, so a client parsing line-by-line cannot observe a torn
  message.
- **every accepted request gets exactly one response.** A notification gets none,
  by definition; everything else — including a handler that raised — is answered.
  An unanswered request would hang the client until its timeout.

Startup is fail-closed on the catalog: :func:`catalog_surface_violations` runs
before the first frame is read, and a catalog that would publish a forbidden
capability aborts the server instead of serving it. A guard that only runs in
tests is not a guard on the thing that ships.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO, Mapping, Optional

from mozyo_bridge import __version__
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.tool_dispatch import (  # noqa: E501
    dispatch_tool_call,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.jsonrpc import (  # noqa: E501
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_NOT_INITIALIZED,
    FrameEncodingError,
    FrameError,
    JsonRpcRequest,
    encode_message,
    error_response,
    parse_frame,
    success_response,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E501
    catalog_surface_violations,
    list_tools_payload,
)

#: The MCP revision this server implements. Negotiation: a client asking for a
#: version in :data:`SUPPORTED_PROTOCOL_VERSIONS` gets that same version back; any
#: other request gets this one, which the lifecycle spec makes the correct answer
#: (respond with a version the server supports, and let the client disconnect if it
#: cannot use it) rather than an error.
PROTOCOL_VERSION = "2025-06-18"

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "mozyo-bridge"

#: Advertised to the client. ``listChanged`` is false: the tool catalog is a frozen
#: table decided at import, so it cannot change within a session and promising a
#: notification we would never send would be a lie the client plans around.
SERVER_CAPABILITIES: Mapping[str, Any] = {"tools": {"listChanged": False}}

#: Shown to the client as ``instructions``. States the surface's boundary in the
#: one place an LLM client is guaranteed to read.
SERVER_INSTRUCTIONS = (
    "Read/plan tools for mozyo-bridge. Every tool is read-only: none delivers a "
    "handoff, writes a durable record, mutates a lane, or runs a command. "
    "`workflow_step_plan` resolves the next safe step without performing it. "
    "`unit_state` reports four independent axes (workflow / runtime / delivery / "
    "health) with per-field source, observed_at and freshness; treat `unknown` "
    "and `unconfirmed` as real answers, and never infer blocked, idle, or "
    "completed from an absent journal update, a silent pane, or an ended turn. "
    "Workflow truth is the Redmine durable record; runtime observation is never "
    "workflow truth, review state, owner approval, or task completion."
)


# --- lifecycle phases (closed) --------------------------------------------- #

#: No usable `initialize` has been received. Only `initialize` and `ping` are
#: answerable; every other request is refused.
PHASE_UNINITIALIZED = "uninitialized"

#: `initialize` succeeded; the client has not yet sent `notifications/initialized`.
#: The spec lets the client send pings here but not ordinary requests.
PHASE_INITIALIZING = "initializing"

#: The handshake completed. The full surface is available.
PHASE_READY = "ready"

#: Params `initialize` must carry. Absent / wrong-typed members are refused
#: rather than defaulted: a client that did not declare its protocol version or
#: identity has not performed the negotiation the phase exists to perform.
REQUIRED_INITIALIZE_PARAMS = ("protocolVersion", "capabilities", "clientInfo")

#: Members the schema's ``Implementation`` type requires inside ``clientInfo``
#: (``Implementation extends BaseMetadata``: ``name: string`` + ``version: string``).
REQUIRED_CLIENT_INFO_FIELDS = ("name", "version")

#: Optional members and their required types, per the schema. Validated so the
#: acceptance boundary is the schema's — no looser, no tighter (review j#102599
#: r3f3). ``BaseMetadata.title`` is the only optional member of ``Implementation``.
OPTIONAL_CLIENT_INFO_TYPES = {"title": str}

#: ``ClientCapabilities``: every member optional. ``roots`` is an object with an
#: optional boolean ``listChanged``; the rest are plain objects.
OPTIONAL_CAPABILITY_TYPES = {
    "experimental": Mapping,
    "roots": Mapping,
    "sampling": Mapping,
    "elicitation": Mapping,
}

#: ``ClientCapabilities.roots``' own optional members.
OPTIONAL_ROOTS_TYPES = {"listChanged": bool}


def _type_violations(
    obj: Mapping[str, Any], expected: Mapping[str, Any], prefix: str
) -> list:
    """Report every optional member of ``obj`` whose type is not ``expected``.

    Absent members are fine — every entry in ``expected`` is optional. Present
    ones must match, because a member the schema types is a member a conforming
    client sends correctly; accepting a wrong type there hides a real defect.
    ``bool`` is checked before ``int``-ish types would matter, and ``Mapping`` is
    used rather than ``dict`` so any mapping the decoder produced is accepted.
    """
    violations = []
    for name, want in expected.items():
        if name not in obj:
            continue
        value = obj[name]
        if want is bool:
            ok = isinstance(value, bool)
        elif want is str:
            ok = isinstance(value, str)
        else:
            ok = isinstance(value, want)
        if not ok:
            violations.append(f"{prefix}.{name}")
    return violations


def _initialize_param_violations(params: Mapping[str, Any]) -> list:
    """Every ``initialize`` param that does not match the MCP schema.

    The acceptance boundary IS the schema:

    - ``clientInfo`` (``Implementation``): ``name`` and ``version`` are required
      strings; ``title`` is an optional string. Neither required member carries a
      length or format constraint, so ``""`` is a conforming value and is
      accepted — the previous non-empty rule was this implementation's own.
    - ``capabilities`` (``ClientCapabilities``): every member optional;
      ``roots`` is an object whose optional ``listChanged`` is a boolean.
    """
    violations: list = []

    client_info = params.get("clientInfo")
    if not isinstance(client_info, Mapping):
        violations.append("clientInfo")
    else:
        violations.extend(
            f"clientInfo.{name}"
            for name in REQUIRED_CLIENT_INFO_FIELDS
            if not isinstance(client_info.get(name), str)
        )
        violations.extend(
            _type_violations(client_info, OPTIONAL_CLIENT_INFO_TYPES, "clientInfo")
        )

    capabilities = params.get("capabilities")
    if not isinstance(capabilities, Mapping):
        violations.append("capabilities")
    else:
        violations.extend(
            _type_violations(capabilities, OPTIONAL_CAPABILITY_TYPES, "capabilities")
        )
        roots = capabilities.get("roots")
        if isinstance(roots, Mapping):
            violations.extend(
                _type_violations(roots, OPTIONAL_ROOTS_TYPES, "capabilities.roots")
            )
        experimental = capabilities.get("experimental")
        if isinstance(experimental, Mapping):
            # The schema types `experimental` as `{ [key: string]: object }`
            # (review j#103251 r4f6): the member itself was checked above, but
            # each of its VALUES must also be an object, and that inner
            # constraint went unvalidated.
            violations.extend(
                f"capabilities.experimental.{name}"
                for name, value in experimental.items()
                if not isinstance(value, Mapping)
            )
    return violations


class CatalogSurfaceError(RuntimeError):
    """The tool catalog would publish a forbidden capability. Startup aborts."""


@dataclass
class McpServer:
    """One stdio MCP session.

    Streams are injected rather than reached for, so a test drives a real session
    over two in-memory pipes with no subprocess and no monkeypatching of ``sys``.
    """

    context: ReadPlanContext
    stdin: IO[str] = field(default_factory=lambda: sys.stdin)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)
    #: The lifecycle phase. A three-state machine, not a boolean (review j#102186
    #: finding_1): a boolean cannot distinguish "initialize has not been sent"
    #: from "initialize succeeded but the client has not confirmed", so an
    #: `initialized` notification arriving first flipped it and opened the whole
    #: tool surface without any handshake.
    _phase: str = field(default=PHASE_UNINITIALIZED, init=False)
    _negotiated_version: str = field(default=PROTOCOL_VERSION, init=False)

    # -- lifecycle ---------------------------------------------------------- #

    def serve(self) -> int:
        """Run the session until stdin reaches EOF. Returns the exit code.

        Never raises for a bad frame: a malformed message is answered (or, for a
        malformed notification, dropped) and the loop continues, because one bad
        frame from a client is not a reason to drop a session that is otherwise
        healthy.
        """
        violations = catalog_surface_violations()
        if violations:
            for violation in violations:
                self._log(f"catalog surface violation: {violation}")
            raise CatalogSurfaceError(
                f"{len(violations)} tool catalog surface violation(s); refusing to serve"
            )
        for line in self.stdin:
            frame = line.strip()
            if not frame:
                continue
            self._handle_frame(frame)
        return 0

    def _handle_frame(self, frame: str) -> None:
        parsed = parse_frame(frame)
        if isinstance(parsed, FrameError):
            if parsed.respondable:
                self._send(error_response(parsed.id, parsed.code, parsed.message))
            else:
                self._log(f"dropped an unanswerable malformed frame: {parsed.message}")
            return
        self._handle_request(parsed)

    def _handle_request(self, request: JsonRpcRequest) -> None:
        """Route one parsed message through the lifecycle phase machine.

        Notifications are separated from requests **first**. That ordering is the
        finding_1 fix: `initialize` used to be answered before the notification
        check, so an `initialize` sent without an id — a notification, which the
        spec says the server MUST NOT reply to — got a response with a null id.
        """
        method = request.method
        if request.is_notification:
            self._handle_notification(method)
            return

        if method == "initialize":
            self._send(self._initialize_response(request))
            return
        if method == "ping":
            # Allowed in every phase: the spec names ping as the one request a
            # client may send before initialization completes.
            self._send(success_response(request.id, {}))
            return
        if self._phase != PHASE_READY:
            self._send(
                error_response(
                    request.id,
                    ERROR_NOT_INITIALIZED,
                    "the session is not initialized; send `initialize` and then the "
                    "`notifications/initialized` notification before any other request",
                    {"phase": self._phase},
                )
            )
            return

        if method == "tools/list":
            self._send(success_response(request.id, {"tools": list_tools_payload()}))
            return
        if method == "tools/call":
            self._send(self._tools_call(request))
            return
        self._send(
            error_response(request.id, ERROR_METHOD_NOT_FOUND, f"unknown method: {method}")
        )

    def _handle_notification(self, method: str) -> None:
        """Handle a notification. Never sends anything — that is the contract."""
        if method == "notifications/initialized":
            if self._phase == PHASE_INITIALIZING:
                self._phase = PHASE_READY
            else:
                # Out of order: the handshake has not reached this point, and the
                # notification carries no id to refuse on. Log and stay put rather
                # than opening the surface — this is exactly the bypass finding_1
                # reported, where the notification alone was enough.
                self._log(
                    "ignored `notifications/initialized` in phase "
                    f"{self._phase}: `initialize` must succeed first"
                )
            return
        if method == "initialize":
            self._log(
                "ignored `initialize` sent as a notification: it is a request and "
                "must carry an id; the server may not reply to a notification"
            )
            return
        # Unknown notifications are ignored by contract: there is no channel to
        # report them on, and refusing a session over one would be worse.

    def _initialize_response(self, request: JsonRpcRequest) -> dict:
        """Validate and apply `initialize`, or refuse it.

        Fail-closed on a malformed or repeated handshake. A second `initialize`
        is refused rather than silently re-negotiating: the client would have no
        way to know which version the already-answered requests were served under.
        """
        if self._phase != PHASE_UNINITIALIZED:
            return error_response(
                request.id,
                ERROR_INVALID_REQUEST,
                "the session is already initialized; `initialize` is sent once",
                {"phase": self._phase},
            )
        params = request.arguments()
        missing = [name for name in REQUIRED_INITIALIZE_PARAMS if name not in params]
        if missing:
            return error_response(
                request.id,
                ERROR_INVALID_PARAMS,
                "`initialize` is missing required params",
                {"missing": missing, "required": list(REQUIRED_INITIALIZE_PARAMS)},
            )
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            # Type only (review j#103251 r4f6): the schema says `string` and
            # nothing else. The previous non-empty/non-whitespace rule was this
            # implementation's own invention; an empty string is a conforming —
            # if unusable — version, and negotiation below answers it with ours.
            return error_response(
                request.id,
                ERROR_INVALID_PARAMS,
                '"protocolVersion" must be a string',
                {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
            )
        # Validate INTO the nested objects, and validate them to *exactly* the
        # schema (review j#102241 r2f1, then j#102599 r3f3). The first fix reached
        # one level in and, in the same stroke, invented a non-empty requirement
        # the schema does not state while still not typing `title` / `roots` /
        # `roots.listChanged`. Both directions of that mismatch are defects: a
        # check the schema does not ask for rejects legal clients, and a check it
        # does ask for is the one that catches real malformation.
        invalid = _initialize_param_violations(params)
        if invalid:
            return error_response(
                request.id,
                ERROR_INVALID_PARAMS,
                "`initialize` params do not match the MCP schema",
                {"invalid": invalid},
            )

        # Version negotiation: echo a version we both support, else answer with
        # ours. The spec makes that the correct reply — the client disconnects if
        # it cannot use it — rather than an error.
        self._negotiated_version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        self._phase = PHASE_INITIALIZING
        return success_response(
            request.id,
            {
                "protocolVersion": self._negotiated_version,
                "capabilities": dict(SERVER_CAPABILITIES),
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "mozyo-bridge local read/plan tools",
                    "version": __version__,
                },
                "instructions": SERVER_INSTRUCTIONS,
            },
        )

    def _tools_call(self, request: JsonRpcRequest) -> dict:
        params = request.arguments()
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return error_response(
                request.id, ERROR_INVALID_PARAMS, '"name" must be a non-empty string'
            )
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            return error_response(
                request.id, ERROR_INVALID_PARAMS, '"arguments" must be an object'
            )
        dispatched = dispatch_tool_call(name, arguments, self.context)
        if dispatched.is_protocol_error:
            failure = dispatched.protocol_error
            return error_response(
                request.id, ERROR_INVALID_PARAMS, failure.message, failure.data
            )
        return success_response(request.id, dispatched.result or {})

    # -- transport ---------------------------------------------------------- #

    def _send(self, message: Mapping[str, Any]) -> None:
        """Write one frame, or degrade to an internal-error frame.

        A response that cannot be rendered is still answered — with an error the
        client can act on — because dropping it would hang the request. If even the
        fallback cannot be rendered, the failure is logged and the request is lost;
        that is the one case where a silent drop beats a corrupt stream.
        """
        try:
            rendered = encode_message(message)
        except FrameEncodingError as exc:
            self._log(f"response could not be encoded: {exc}")
            request_id = message.get("id") if isinstance(message, Mapping) else None
            try:
                rendered = encode_message(
                    error_response(
                        request_id, ERROR_INTERNAL, "the response could not be encoded"
                    )
                )
            except FrameEncodingError:  # pragma: no cover - not reachable in practice
                return
        self.stdout.write(rendered + "\n")
        self.stdout.flush()

    def _log(self, message: str) -> None:
        """Diagnostics go to stderr only; stdout is reserved for MCP frames."""
        try:
            self.stderr.write(f"mozyo-bridge mcp: {message}\n")
            self.stderr.flush()
        except Exception:  # noqa: BLE001 - a closed stderr never breaks the session
            pass


def serve_stdio(
    *,
    repo_root: Optional[Path] = None,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
) -> int:
    """Start a stdio MCP session for ``repo_root`` and run it to EOF."""
    context = ReadPlanContext(repo_root=Path(repo_root or Path.cwd()).resolve())
    server = McpServer(
        context=context,
        stdin=stdin if stdin is not None else sys.stdin,
        stdout=stdout if stdout is not None else sys.stdout,
        stderr=stderr if stderr is not None else sys.stderr,
    )
    return server.serve()


__all__ = (
    "CatalogSurfaceError",
    "McpServer",
    "PHASE_INITIALIZING",
    "PHASE_READY",
    "PHASE_UNINITIALIZED",
    "PROTOCOL_VERSION",
    "REQUIRED_INITIALIZE_PARAMS",
    "SERVER_CAPABILITIES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "serve_stdio",
)
