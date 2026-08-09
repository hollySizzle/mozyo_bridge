"""Regression pins for the #15151 review findings (Redmine j#102186, verdict j#102195).

One class per finding, each reproducing the reported defect as it was reported and
asserting the fixed behavior. These live in ``tests/regressions`` rather than beside
the feature tests because their job is to keep *these specific defects* from
returning, independently of how the feature's own specs are later reorganized.

All five were accepted after independent reproduction; none was disputed.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    cli_workflow,
    herdr_workflow_step,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E402,E501
    FRESHNESS_FRESH,
    FRESHNESS_UNKNOWN,
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mcp_server import (  # noqa: E402,E501
    PHASE_INITIALIZING,
    PHASE_READY,
    PHASE_UNINITIALIZED,
    REQUIRED_INITIALIZE_PARAMS,
    McpServer,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
    run_docs_resolve,
    run_workflow_step_plan,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E402,E501
    UnitFacts,
    compose_unit_state,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.blocker_claim import (  # noqa: E402,E501
    latest_blocker_claim,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.jsonrpc import (  # noqa: E402,E501
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_NOT_INITIALIZED,
    FrameError,
    JsonRpcRequest,
    parse_frame,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.repo_path import (  # noqa: E402,E501
    PATH_ABSOLUTE,
    PATH_ESCAPES_REPO,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E402,E501
    UnitRecord,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E402,E501
    BlockedClaim,
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "regression", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def session(frames, *, repo_root: Path = ROOT):
    """Run one in-process stdio session; return (responses, stderr, server)."""
    out, err = io.StringIO(), io.StringIO()
    server = McpServer(
        context=ReadPlanContext(repo_root=repo_root, redmine_live=False),
        stdin=io.StringIO("\n".join(json.dumps(f) for f in frames) + "\n"),
        stdout=out,
        stderr=err,
    )
    server.serve()
    responses = [json.loads(line) for line in out.getvalue().splitlines() if line]
    return responses, err.getvalue(), server


class Finding1LifecycleTests(unittest.TestCase):
    """MCP lifecycle could be skipped, and `initialize` input was unvalidated."""

    def test_initialized_notification_alone_does_not_open_the_tool_surface(self) -> None:
        """The reported bypass: initialized-before-initialize, then tools/list."""
        responses, _, server = session(
            [INITIALIZED, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )
        self.assertEqual(responses[0]["error"]["code"], ERROR_NOT_INITIALIZED)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)

    def test_initialize_with_empty_params_is_refused(self) -> None:
        responses, _, _ = session(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        )
        error = responses[0]["error"]
        self.assertEqual(error["code"], ERROR_INVALID_PARAMS)
        self.assertEqual(sorted(error["data"]["missing"]), sorted(REQUIRED_INITIALIZE_PARAMS))

    def test_each_required_initialize_param_is_individually_required(self) -> None:
        for missing in REQUIRED_INITIALIZE_PARAMS:
            params = {k: v for k, v in INITIALIZE["params"].items() if k != missing}
            responses, _, _ = session(
                [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}]
            )
            self.assertEqual(responses[0]["error"]["code"], ERROR_INVALID_PARAMS, missing)

    def test_wrong_typed_initialize_params_are_refused(self) -> None:
        for key, bad in (
            ("protocolVersion", ""),
            ("protocolVersion", 5),
            ("capabilities", "none"),
            ("clientInfo", []),
        ):
            params = dict(INITIALIZE["params"])
            params[key] = bad
            responses, _, _ = session(
                [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}]
            )
            self.assertEqual(
                responses[0]["error"]["code"], ERROR_INVALID_PARAMS, f"{key}={bad!r}"
            )

    def test_initialize_sent_as_a_notification_gets_no_response(self) -> None:
        """The spec forbids replying to a notification; the old code replied with id null."""
        responses, stderr, server = session(
            [{"jsonrpc": "2.0", "method": "initialize", "params": INITIALIZE["params"]}]
        )
        self.assertEqual(responses, [])
        self.assertIn("initialize", stderr)
        self.assertEqual(server._phase, PHASE_UNINITIALIZED)

    def test_a_second_initialize_is_refused(self) -> None:
        responses, _, _ = session([INITIALIZE, INITIALIZE])
        self.assertEqual(responses[1]["error"]["code"], ERROR_INVALID_REQUEST)

    def test_the_full_handshake_reaches_ready_and_serves_tools(self) -> None:
        responses, _, server = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        )
        self.assertEqual(server._phase, PHASE_READY)
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)

    def test_requests_are_refused_between_initialize_and_initialized(self) -> None:
        responses, _, server = session(
            [INITIALIZE, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        )
        self.assertEqual(server._phase, PHASE_INITIALIZING)
        self.assertEqual(responses[1]["error"]["code"], ERROR_NOT_INITIALIZED)

    def test_ping_is_answerable_in_every_phase(self) -> None:
        """The one request the spec allows before initialization completes."""
        responses, _, _ = session([{"jsonrpc": "2.0", "id": 9, "method": "ping"}])
        self.assertEqual(responses[0]["result"], {})


class Finding2BackendSelectionTests(unittest.TestCase):
    """`workflow_step_plan` called the tmux rail regardless of the configured backend."""

    def _run(self, *, herdr_active: bool):
        calls = {"require_tmux": 0, "herdr": 0}

        def fake_require_tmux():
            calls["require_tmux"] += 1

        def fake_current_pane():
            return "%1"

        def fake_discover():
            return []

        def fake_herdr(_args):
            calls["herdr"] += 1

            class Outcome:
                ok = True

                def as_payload(self):
                    return {"next_action": "herdr", "state": "resolved"}

            return Outcome()

        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_entrypoint_preflight.herdr_backend_active",
            return_value=herdr_active,
        ), patch.object(cli_workflow, "require_tmux", fake_require_tmux), patch.object(
            cli_workflow, "current_pane", fake_current_pane
        ), patch.object(
            cli_workflow, "_discover_candidates", fake_discover
        ), patch.object(
            herdr_workflow_step, "resolve_herdr_step_outcome", fake_herdr
        ):
            outcome = run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        return outcome, calls

    def test_a_herdr_repo_resolves_herdr_natively_and_never_touches_tmux(self) -> None:
        outcome, calls = self._run(herdr_active=True)
        self.assertEqual(calls["herdr"], 1)
        self.assertEqual(calls["require_tmux"], 0)
        self.assertEqual(outcome.payload["backend"], "herdr")

    def test_a_tmux_repo_still_uses_the_tmux_rail(self) -> None:
        outcome, calls = self._run(herdr_active=False)
        self.assertEqual(calls["herdr"], 0)
        self.assertEqual(calls["require_tmux"], 1)
        self.assertEqual(outcome.payload["backend"], "tmux")

    def test_the_plan_is_never_executed_on_either_backend(self) -> None:
        for herdr in (True, False):
            outcome, _ = self._run(herdr_active=herdr)
            self.assertFalse(outcome.payload["executed"], herdr)
            self.assertEqual(outcome.payload["execution"], "plan_only", herdr)

    def test_the_shared_resolver_reads_only_repo_off_its_namespace_shim(self) -> None:
        """Pins the one CLI-shaped seam: if the resolver grows a field, this fails."""
        seen = {}

        def capture(args):
            seen["attrs"] = {
                k for k in vars(args) if not k.startswith("_")
            }

            class Outcome:
                ok = True

                def as_payload(self):
                    return {}

            return Outcome()

        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_entrypoint_preflight.herdr_backend_active",
            return_value=True,
        ), patch.object(herdr_workflow_step, "resolve_herdr_step_outcome", capture):
            run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        self.assertEqual(seen["attrs"], {"repo"})


class Finding3RepoRelativePathTests(unittest.TestCase):
    """`docs_resolve` accepted absolute / escaping paths and leaked the repo root."""

    def _context(self) -> ReadPlanContext:
        return ReadPlanContext(repo_root=ROOT)

    def test_an_absolute_path_is_refused_with_a_fixed_reason(self) -> None:
        outcome = run_docs_resolve({"paths": ["/etc/passwd"]}, self._context())
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["error"], "invalid_path")
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ABSOLUTE)

    def test_a_repo_escaping_path_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["../outside/private.py"]}, self._context())
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ESCAPES_REPO)

    def test_a_path_that_escapes_after_normalization_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["src/../../x.py"]}, self._context())
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ESCAPES_REPO)

    def test_a_windows_absolute_path_is_refused(self) -> None:
        outcome = run_docs_resolve({"paths": ["C:\\Users\\x"]}, self._context())
        self.assertEqual(outcome.payload["rejected"][0]["reason"], PATH_ABSOLUTE)

    def test_no_refusal_leaks_the_servers_absolute_repo_root(self) -> None:
        """The reported leak: the resolver's ValueError named the server's own root."""
        root = str(ROOT)
        for bad in ("/etc/passwd", "../outside/private.py", "C:\\x", "src/../../x.py"):
            outcome = run_docs_resolve({"paths": [bad]}, self._context())
            self.assertNotIn(root, repr(outcome.payload), bad)
            self.assertNotIn(root, outcome.summary, bad)

    def test_every_bad_path_is_reported_not_just_the_first(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["/abs/one", "../two", "src/ok.py"]}, self._context()
        )
        self.assertEqual(len(outcome.payload["rejected"]), 2)

    def test_a_catalog_failure_reports_a_fixed_reason_not_exception_text(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["src/x.py"]},
            ReadPlanContext(repo_root=ROOT, catalog_path="/nonexistent/catalog.yaml"),
        )
        self.assertTrue(outcome.is_error)
        self.assertIn("reason", outcome.payload)
        self.assertNotIn("/nonexistent/catalog.yaml", repr(outcome.payload))

    def test_a_valid_repo_relative_path_still_resolves(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["src/mozyo_bridge/application/cli.py"]}, self._context()
        )
        self.assertFalse(outcome.is_error)
        self.assertEqual(len(outcome.payload["resolutions"]), 1)


class Finding4RequestIdTests(unittest.TestCase):
    """Boolean ids were accepted and an explicit null id was read as a notification."""

    def test_a_boolean_id_is_an_invalid_request(self) -> None:
        """`bool` subclasses `int`, so the old isinstance check let `true` through."""
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": True, "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertEqual(parsed.code, ERROR_INVALID_REQUEST)
        self.assertTrue(parsed.respondable)

    def test_an_explicit_null_id_is_a_request_not_a_notification(self) -> None:
        """The spec: a Notification is a Request *without* an id member."""
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertTrue(parsed.has_id)
        self.assertFalse(parsed.is_notification)

    def test_an_absent_id_member_is_a_notification(self) -> None:
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
        self.assertIsInstance(parsed, JsonRpcRequest)
        self.assertFalse(parsed.has_id)
        self.assertTrue(parsed.is_notification)

    def test_a_null_id_request_is_answered_with_a_null_id_response(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "id": None, "method": "ping"}]
        )
        self.assertEqual(len(responses), 2)
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(responses[1]["result"], {})

    def test_a_notification_ping_is_not_answered(self) -> None:
        responses, _, _ = session(
            [INITIALIZE, INITIALIZED, {"jsonrpc": "2.0", "method": "ping"}]
        )
        self.assertEqual(len(responses), 1)

    def test_a_malformed_notification_is_still_not_answered(self) -> None:
        """A refused frame with no id member must stay unanswered."""
        parsed = parse_frame(json.dumps({"jsonrpc": "1.0", "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertFalse(parsed.respondable)

    def test_a_malformed_null_id_request_is_answered(self) -> None:
        parsed = parse_frame(json.dumps({"jsonrpc": "1.0", "id": None, "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)
        self.assertTrue(parsed.respondable)

    def test_a_float_id_is_still_refused(self) -> None:
        parsed = parse_frame(json.dumps({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}))
        self.assertIsInstance(parsed, FrameError)


BLOCKED_NOTE = """## Sublane park

- state: blocked
- durable_anchor: #15151 j#10
- blocked_by: dependency #15149 is not closed
- resume_condition: close #15149 and re-dispatch
- resume_owner: coordinator
"""

RESUMED_NOTE = "## Gate: progress_log\n\n- state: active\n- note: work resumed\n"
UNRELATED_NOTE = "## Gate: implementation_done\n\n- note: no state field here\n"

UNIT = UnitRecord(workspace_id="w", lane_id="l", project_id="p")
CLAIM = BlockedClaim(
    blocker_source=SOURCE_REDMINE,
    reason="dependency #15149 is not closed",
    resume_condition="close #15149 and re-dispatch",
    durable_anchor="#15151 j#10",
    observed_at="2026-08-10T00:00:00Z",
    freshness=FRESHNESS_FRESH,
)


class Finding5StaleBlockedClaimTests(unittest.TestCase):
    """A recorded block never cleared, and its claim carried no observation envelope."""

    def test_a_later_non_blocked_state_declaration_clears_the_claim(self) -> None:
        self.assertIsNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", RESUMED_NOTE)], issue_id="15151"
            )
        )

    def test_a_later_unrelated_journal_does_not_hide_a_standing_block(self) -> None:
        self.assertIsNotNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", UNRELATED_NOTE)], issue_id="15151"
            )
        )

    def test_a_conflicting_later_state_declaration_clears_the_claim(self) -> None:
        """A record declaring `state:` twice with different values sustains nothing."""
        conflicted = "- state: blocked\n- state: active\n"
        self.assertIsNone(
            latest_blocker_claim(
                [("10", BLOCKED_NOTE), ("11", conflicted)], issue_id="15151"
            )
        )

    def test_the_claim_carries_the_observation_envelope_it_was_read_with(self) -> None:
        claim = latest_blocker_claim(
            [("10", BLOCKED_NOTE)],
            issue_id="15151",
            observed_at="2026-08-10T12:00:00Z",
            freshness=FRESHNESS_FRESH,
        )
        self.assertEqual(claim.observed_at, "2026-08-10T12:00:00Z")
        self.assertEqual(claim.freshness, FRESHNESS_FRESH)

    def test_a_claim_is_dropped_when_the_current_gate_is_not_blocked(self) -> None:
        """The durable fold is the authority on whether the block is still in force."""
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="implementing",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=CLAIM,
            ),
        )
        self.assertIsNone(report.workflow.blocked)
        self.assertEqual(report.workflow.state.value, "implementing")
        self.assertIn("no longer reports blocked", report.workflow.state.note or "")

    def test_a_claim_is_dropped_when_the_current_gate_is_unreadable(self) -> None:
        """Fail-closed: an unconfirmable block is not reported as current."""
        report = compose_unit_state(
            UNIT, UnitFacts(workflow_readable=False, workflow_blocked=CLAIM)
        )
        self.assertIsNone(report.workflow.blocked)

    def test_a_claim_survives_when_the_current_gate_agrees(self) -> None:
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="blocked",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=CLAIM,
            ),
        )
        self.assertIsNotNone(report.workflow.blocked)
        self.assertEqual(report.workflow.state.value, "blocked")
        self.assertEqual(report.workflow.blocked.freshness, FRESHNESS_FRESH)
        self.assertIsNotNone(report.workflow.blocked.observed_at)

    def test_a_claim_with_no_envelope_is_still_reported_as_unknown_freshness(self) -> None:
        import dataclasses

        bare = dataclasses.replace(CLAIM, observed_at=None, freshness=FRESHNESS_UNKNOWN)
        report = compose_unit_state(
            UNIT,
            UnitFacts(
                workflow_state="blocked",
                workflow_readable=True,
                workflow_observed_at="2026-08-10T00:00:00Z",
                workflow_freshness=FRESHNESS_FRESH,
                workflow_blocked=bare,
            ),
        )
        self.assertEqual(report.workflow.blocked.freshness, FRESHNESS_UNKNOWN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
