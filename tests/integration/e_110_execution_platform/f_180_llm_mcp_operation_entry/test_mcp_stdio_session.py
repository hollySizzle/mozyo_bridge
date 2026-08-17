"""Protocol lifecycle over a real stdio session (Redmine #15161 / #15163).

Drives :class:`McpServer` over in-memory pipes for the framing / lifecycle
assertions, and — separately — spawns the **installed package** so the acceptance
"startable and stoppable non-interactively from the installed package" is proved
against the real entry point rather than an import.

The invariant every one of these protects is the same: an MCP client parses stdout
line by line. Anything on stdout that is not a frame, any frame containing a
newline, and any accepted request left unanswered breaks the client in a way that
is silent from this side.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E402,E501
    PROTOCOL_VERSION,
    SERVER_NAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpServer,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.jsonrpc import (  # noqa: E402,E501
    ERROR_INVALID_PARAMS,
    ERROR_METHOD_NOT_FOUND,
    ERROR_NOT_INITIALIZED,
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "spec", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class SessionHarness:
    """One in-process stdio session over string buffers."""

    def __init__(self, frames, *, repo_root: Path = ROOT) -> None:
        payload = "\n".join(
            f if isinstance(f, str) else json.dumps(f) for f in frames
        )
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.server = McpServer(
            context=ReadPlanContext(repo_root=repo_root, redmine_live=False),
            stdin=io.StringIO(payload + "\n"),
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.exit_code = self.server.serve()

    @property
    def lines(self) -> list:
        return [line for line in self.stdout.getvalue().split("\n") if line]

    @property
    def responses(self) -> list:
        return [json.loads(line) for line in self.lines]

    def by_id(self, request_id):
        for response in self.responses:
            if response.get("id") == request_id:
                return response
        return None


def handshake(*frames) -> SessionHarness:
    return SessionHarness([INITIALIZE, INITIALIZED, *frames])


class LifecycleTests(unittest.TestCase):
    def test_initialize_returns_the_negotiated_version_and_server_info(self) -> None:
        session = SessionHarness([INITIALIZE])
        result = session.by_id(1)["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], SERVER_NAME)
        self.assertIn("tools", result["capabilities"])
        self.assertIn("instructions", result)

    def test_a_supported_older_version_is_echoed_back(self) -> None:
        older = dict(INITIALIZE)
        older["params"] = dict(INITIALIZE["params"], protocolVersion="2024-11-05")
        session = SessionHarness([older])
        self.assertIn("2024-11-05", SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual(session.by_id(1)["result"]["protocolVersion"], "2024-11-05")

    def test_an_unsupported_version_gets_this_server_version_not_an_error(self) -> None:
        """The lifecycle spec says respond with a version we support."""
        odd = dict(INITIALIZE)
        odd["params"] = dict(INITIALIZE["params"], protocolVersion="1.0.0")
        session = SessionHarness([odd])
        response = session.by_id(1)
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)

    def test_the_initialized_notification_gets_no_response(self) -> None:
        session = SessionHarness([INITIALIZE, INITIALIZED])
        self.assertEqual(len(session.responses), 1)

    def test_a_request_before_initialization_is_refused(self) -> None:
        session = SessionHarness([{"jsonrpc": "2.0", "id": 5, "method": "tools/list"}])
        self.assertEqual(session.by_id(5)["error"]["code"], ERROR_NOT_INITIALIZED)

    def test_ping_is_allowed_before_initialization(self) -> None:
        session = SessionHarness([{"jsonrpc": "2.0", "id": 6, "method": "ping"}])
        self.assertEqual(session.by_id(6)["result"], {})

    def test_the_session_ends_at_eof_without_a_signal(self) -> None:
        session = SessionHarness([INITIALIZE])
        self.assertEqual(session.exit_code, 0)

    def test_an_empty_stdin_is_a_clean_zero_exit(self) -> None:
        session = SessionHarness([])
        self.assertEqual(session.exit_code, 0)
        self.assertEqual(session.lines, [])

    def test_an_unknown_method_is_method_not_found(self) -> None:
        session = handshake({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
        self.assertEqual(session.by_id(7)["error"]["code"], ERROR_METHOD_NOT_FOUND)

    def test_an_unknown_notification_is_ignored_without_a_response(self) -> None:
        session = handshake({"jsonrpc": "2.0", "method": "notifications/cancelled"})
        self.assertEqual(len(session.responses), 1)  # only the initialize result


class ToolsTests(unittest.TestCase):
    def test_tools_list_publishes_the_declared_catalog(self) -> None:
        # Contract updated by #15152 (was: exactly the four read-only tools):
        # the three declared mutating tools are now published, honestly
        # annotated as not read-only.
        session = handshake({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = session.by_id(2)["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in tools],
            [
                "docs_resolve",
                "workflow_glance",
                "workflow_step_plan",
                "unit_state",
                "handoff_send",
                "handoff_reply",
                "sublane_start",
            ],
        )
        mutating = {"handoff_send", "handoff_reply", "sublane_start"}
        for tool in tools:
            self.assertEqual(
                tool["annotations"]["readOnlyHint"],
                tool["name"] not in mutating,
                tool["name"],
            )

    def test_a_tool_call_returns_content_and_structured_content(self) -> None:
        session = handshake(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "docs_resolve",
                    "arguments": {"paths": ["src/mozyo_bridge/application/cli.py"]},
                },
            }
        )
        result = session.by_id(3)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("resolutions", result["structuredContent"])

    def test_an_unknown_tool_is_a_protocol_error_naming_the_available_tools(self) -> None:
        # `handoff_send` became a published tool in #15152; the unknown-name
        # probe now uses a name that stays outside the closed vocabulary.
        session = handshake(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "sublane_retire", "arguments": {}},
            }
        )
        error = session.by_id(4)["error"]
        self.assertEqual(error["code"], ERROR_INVALID_PARAMS)
        self.assertIn("available_tools", error["data"])
        self.assertNotIn("sublane_retire", error["data"]["available_tools"])

    def test_malformed_arguments_are_a_protocol_error_listing_the_violations(self) -> None:
        session = handshake(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "unit_state", "arguments": {"unit": {}}},
            }
        )
        error = session.by_id(8)["error"]
        self.assertEqual(error["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(len(error["data"]["violations"]), 3)

    def test_a_missing_tool_name_is_a_protocol_error(self) -> None:
        session = handshake(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}}
        )
        self.assertEqual(session.by_id(9)["error"]["code"], ERROR_INVALID_PARAMS)

    def test_non_object_arguments_are_a_protocol_error(self) -> None:
        session = handshake(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "docs_resolve", "arguments": ["a"]},
            }
        )
        self.assertEqual(session.by_id(10)["error"]["code"], ERROR_INVALID_PARAMS)

    def test_a_source_failure_is_a_tool_error_not_a_protocol_error(self) -> None:
        """The split that lets a caller see the structured reason."""
        session = SessionHarness(
            [
                INITIALIZE,
                INITIALIZED,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "docs_resolve", "arguments": {"paths": ["a"]}},
                },
            ],
            repo_root=Path("/nonexistent-repo-root"),
        )
        response = session.by_id(11)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("error", response["result"]["structuredContent"])


class StreamDisciplineTests(unittest.TestCase):
    def test_every_stdout_line_is_exactly_one_json_frame(self) -> None:
        session = handshake(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        )
        for line in session.lines:
            parsed = json.loads(line)  # must not raise
            self.assertEqual(parsed["jsonrpc"], "2.0")

    def test_a_malformed_frame_does_not_kill_the_session(self) -> None:
        session = handshake(
            "{not json",
            {"jsonrpc": "2.0", "id": 12, "method": "ping"},
        )
        self.assertIsNotNone(session.by_id(12))
        self.assertEqual(session.exit_code, 0)

    def test_a_malformed_frame_never_writes_prose_to_stdout(self) -> None:
        session = handshake("{not json")
        for line in session.lines:
            json.loads(line)

    def test_diagnostics_go_to_stderr_only(self) -> None:
        """A step-plan refusal prints via ``die``; stdout must stay clean."""
        session = handshake(
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "workflow_step_plan", "arguments": {}},
            }
        )
        for line in session.lines:
            json.loads(line)
        self.assertIsNotNone(session.by_id(13))

    def test_every_request_receives_exactly_one_response(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(20, 30)
        ]
        session = handshake(*requests)
        answered = [r["id"] for r in session.responses if r.get("id") != 1]
        self.assertEqual(sorted(answered), list(range(20, 30)))


class InstalledPackageSmokeTests(unittest.TestCase):
    """The acceptance: non-interactive start / stop from the installed package."""

    def _run(self, frames, *, argv) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        env.pop("TMUX_PANE", None)
        payload = "".join(json.dumps(f) + "\n" for f in frames)
        return subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            timeout=120,
        )

    def test_the_cli_entry_point_serves_a_session_and_exits_at_eof(self) -> None:
        completed = self._run(
            [
                INITIALIZE,
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            argv=[sys.executable, "-m", "mozyo_bridge", "mcp", "serve"],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.split("\n") if line]
        self.assertEqual(len(lines), 2)
        listed = json.loads(lines[1])["result"]["tools"]
        self.assertEqual(len(listed), 7)  # 4 read/plan + 3 declared mutating (#15152)

    def test_stdout_carries_no_non_frame_byte(self) -> None:
        completed = self._run(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
            argv=[sys.executable, "-m", "mozyo_bridge", "mcp", "serve"],
        )
        for line in completed.stdout.split("\n"):
            if line:
                json.loads(line)

    def test_the_operator_catalog_view_reports_no_surface_violation(self) -> None:
        completed = self._run(
            [],
            argv=[
                sys.executable,
                "-m",
                "mozyo_bridge",
                "mcp",
                "tools",
                "--json",
            ],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            len(payload["tools"]), 7
        )  # 4 read/plan + 3 declared mutating (#15152)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
