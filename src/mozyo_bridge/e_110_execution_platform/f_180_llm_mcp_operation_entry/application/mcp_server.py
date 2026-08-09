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
    _initialized: bool = field(default=False, init=False)
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
        method = request.method
        if method == "initialize":
            self._send(
                success_response(request.id, self._initialize(request.arguments()))
            )
            return
        if method == "notifications/initialized":
            self._initialized = True
            return
        if method.startswith("notifications/"):
            # Unknown notifications are ignored by contract: there is no channel to
            # report them on, and refusing a session over one would be worse.
            return
        if request.is_notification:
            return

        if not self._initialized and method != "ping":
            self._send(
                error_response(
                    request.id,
                    ERROR_NOT_INITIALIZED,
                    "the session is not initialized; send `initialize` then the "
                    "`notifications/initialized` notification first",
                )
            )
            return

        if method == "ping":
            self._send(success_response(request.id, {}))
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

    def _initialize(self, params: Mapping[str, Any]) -> dict:
        requested = str(params.get("protocolVersion", "") or "")
        self._negotiated_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": self._negotiated_version,
            "capabilities": dict(SERVER_CAPABILITIES),
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "mozyo-bridge local read/plan tools",
                "version": __version__,
            },
            "instructions": SERVER_INSTRUCTIONS,
        }

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
    "PROTOCOL_VERSION",
    "SERVER_CAPABILITIES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "serve_stdio",
)
