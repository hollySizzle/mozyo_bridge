"""`workflow step` CLI integration tests (Redmine #12755).

Covers the standard-entrypoint behavior the design fixes
(``vibes/docs/logics/workflow-step-command-design.md``):

- the family registers as the standard ``workflow step`` entrypoint;
- ``--dry-run`` reports the resolved outcome without dispatching a primitive;
- ``--json`` emits exactly one structured outcome envelope;
- a fail-closed lane (anchor-required / blocked) returns rc 1 with the next owner;
- an executable forward leg dispatches the internal primitive (the AI never types
  ``project-gateway consult`` / a ``%pane`` / a rail), and the dispatch reaches the
  gated ``orchestrate_handoff``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import commands
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (
    cli_project_gateway_consult,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    TargetCandidate,
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow,
)

REPO = "/work/repo"
PROJECT = "cloud-drive"


def _cand(pane_id, *, role="codex", project_scope="", lane_kind=""):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source="pane_option",
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="gw",
        window_name="w",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws",
        workspace_label="ws",
        lane_id="lane",
        lane_label="lane",
        repo_short="repo",
        repo_root=REPO,
        cwd=REPO,
        host="host",
        view_kind=VIEW_KIND_COCKPIT_PANE,
        branch=None,
        lane_kind=lane_kind,
        delegation_parent="",
        project_scope=project_scope,
        project_path="",
        project_label="",
    )


def _args(**overrides):
    base = dict(
        dry_run=False,
        as_json=False,
        session=None,
        issue=None,
        journal=None,
        callback=None,
        store_path=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# The persisted runtime store is read fail-open (#13291). These pre-#13291 tests assert
# the live-only step behavior, so pin the store to absent to stay hermetic from the home
# store (dedicated reconcile coverage lives in test_workflow_step_reconcile_cli.py).
_ABSENT_STORE = (None, "store_absent")


def _run(args, candidates, *, self_pane="%self"):
    out = io.StringIO()
    with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "_herdr_step_preflight", lambda _a: None
    ), patch.object(
        cli_workflow, "current_pane", lambda: self_pane
    ), patch.object(
        cli_workflow, "_discover_candidates", return_value=candidates
    ), patch.object(
        cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
    ), contextlib.redirect_stdout(out):
        rc = cli_workflow.cmd_workflow_step(args)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_workflow_step_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "step", "--dry-run", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_step")
        self.assertTrue(ns.dry_run)
        self.assertTrue(ns.as_json)


class DryRunTest(unittest.TestCase):
    def test_dry_run_json_envelope_is_single_object(self):
        rc, text = _run(
            _args(dry_run=True, as_json=True),
            [_cand("%self"), _cand("%gw", project_scope=PROJECT)],
        )
        payload = json.loads(text)  # must parse as exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["execution"], "dry_run")
        self.assertEqual(payload["reason"], "consultation_ready")
        self.assertEqual(payload["next_owner"], "parent")
        self.assertEqual(payload["primitive"], "project_gateway_consult")

    def test_dry_run_does_not_dispatch_primitive(self):
        # If a primitive were dispatched, build_parser/orchestrate would be hit.
        with patch.object(cli_workflow, "_execute_primitive") as exec_mock:
            rc, _ = _run(
                _args(dry_run=True),
                [_cand("%self"), _cand("%gw", project_scope=PROJECT)],
            )
        exec_mock.assert_not_called()
        self.assertEqual(rc, 0)


class FailClosedTest(unittest.TestCase):
    def test_child_anchor_required_rc1(self):
        rc, text = _run(
            _args(),
            [_cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator")],
        )
        self.assertEqual(rc, 1)
        self.assertIn("anchor_required", text)
        self.assertIn("next_owner: child", text)

    def test_unsafe_self_lane_rc1(self):
        rc, text = _run(_args(), [_cand("%other", project_scope=PROJECT)])
        self.assertEqual(rc, 1)
        self.assertIn("self_lane_unresolved", text)


class ExecuteForwardLegTest(unittest.TestCase):
    """The grandparent forward leg dispatches `project-gateway consult` internally."""

    def test_consult_is_dispatched_and_reaches_orchestrate(self):
        gateway = _cand("%gw", project_scope=PROJECT)
        captured: dict[str, object] = {}

        def fake_orchestrate(args, **kwargs):
            captured["target"] = getattr(args, "target", None)
            captured["to"] = getattr(args, "to", None)
            captured["target_repo"] = getattr(args, "target_repo", None)
            captured["target_project"] = getattr(args, "target_project", None)
            captured["ticketless_consultation"] = kwargs.get("ticketless_consultation")
            return 0

        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "_herdr_step_preflight", lambda _a: None
    ), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[_cand("%self"), gateway]
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
        ), patch.object(
            cli_project_gateway_consult, "require_tmux", lambda: None
        ), patch.object(
            cli_project_gateway_consult, "_discover_candidates", return_value=[gateway]
        ), patch.object(
            cli_project_gateway_consult, "orchestrate_handoff", side_effect=fake_orchestrate
        ), contextlib.redirect_stdout(out):
            rc = cli_workflow.cmd_workflow_step(_args())

        self.assertEqual(rc, 0)
        # The pane was resolved by the primitive, not typed by the caller.
        self.assertEqual(captured["target"], "%gw")
        self.assertEqual(captured["to"], "codex")
        self.assertEqual(captured["target_repo"], REPO)
        self.assertEqual(captured["target_project"], PROJECT)
        self.assertTrue(captured["ticketless_consultation"])
        self.assertIn("execution: executed", out.getvalue())

    def test_execute_json_is_single_envelope(self):
        gateway = _cand("%gw", project_scope=PROJECT)
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "_herdr_step_preflight", lambda _a: None
    ), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[_cand("%self"), gateway]
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
        ), patch.object(
            cli_project_gateway_consult, "require_tmux", lambda: None
        ), patch.object(
            cli_project_gateway_consult, "_discover_candidates", return_value=[gateway]
        ), patch.object(
            cli_project_gateway_consult, "orchestrate_handoff", side_effect=lambda a, **k: 0
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli_workflow.cmd_workflow_step(_args(as_json=True))

        payload = json.loads(out.getvalue())  # exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["execution"], "executed")
        self.assertEqual(payload["primitive_rc"], 0)
        self.assertIn("primitive_output", payload)


class StandardSurfaceHelpTest(unittest.TestCase):
    """Redmine #12755 review j#67579 finding 3: standard help is the three-command surface."""

    def _step_help(self) -> str:
        parser = build_parser()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                parser.parse_args(["workflow", "step", "--help"])
        except SystemExit:
            pass
        return buf.getvalue()

    def test_help_shows_only_dry_run_and_json(self):
        help_text = self._step_help()
        self.assertIn("--dry-run", help_text)
        self.assertIn("--json", help_text)

    def test_help_hides_debug_and_escape_knobs(self):
        help_text = self._step_help()
        for hidden in ("--session", "--issue", "--journal", "--callback"):
            self.assertNotIn(hidden, help_text)

    def test_hidden_knobs_still_parse(self):
        # SUPPRESS hides from --help but the flags stay functional.
        parser = build_parser()
        ns = parser.parse_args(
            ["workflow", "step", "--issue", "12755", "--journal", "67549", "--callback", "blocked"]
        )
        self.assertEqual(ns.issue, "12755")
        self.assertEqual(ns.journal, "67549")
        self.assertEqual(ns.callback, "blocked")


class ExecuteWorkerDispatchTest(unittest.TestCase):
    """The child lane dispatches the anchored `handoff send` when anchor + worker resolve."""

    def test_worker_dispatch_is_executed(self):
        child = _cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator")
        worker = _cand("%wk", role="claude", project_scope=PROJECT)
        captured: dict[str, object] = {}

        def fake_orchestrate(args, **kwargs):
            captured["target"] = getattr(args, "target", None)
            captured["to"] = getattr(args, "to", None)
            captured["kind"] = getattr(args, "kind", None)
            captured["source"] = getattr(args, "source", None)
            captured["issue"] = getattr(args, "issue", None)
            captured["journal"] = getattr(args, "journal", None)
            return 0

        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "_herdr_step_preflight", lambda _a: None
    ), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[child, worker]
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
        ), patch.object(
            commands, "orchestrate_handoff", side_effect=fake_orchestrate
        ), contextlib.redirect_stdout(out):
            rc = cli_workflow.cmd_workflow_step(_args(issue="12755", journal="67549"))

        self.assertEqual(rc, 0)
        self.assertEqual(captured["target"], "%wk")
        self.assertEqual(captured["to"], "claude")
        self.assertEqual(captured["kind"], "implementation_request")
        self.assertEqual(captured["source"], "redmine")
        self.assertEqual(captured["issue"], "12755")
        self.assertEqual(captured["journal"], "67549")
        self.assertIn("execution: executed", out.getvalue())

    def test_worker_missing_fails_closed_without_dispatch(self):
        child = _cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator")
        with patch.object(commands, "orchestrate_handoff") as orch:
            rc, text = _run(_args(issue="12755"), [child])
        orch.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertIn("worker_missing", text)


class ExecuteCallbackTest(unittest.TestCase):
    """A determined callback dispatches `handoff ticketless-callback` internally."""

    def test_callback_is_executed_with_resolved_caller_target(self):
        gateway = _cand("%self", project_scope=PROJECT)
        grandparent = _cand("%gp")  # caller: strong codex, no scope
        captured: dict[str, object] = {}

        def fake_orchestrate(args, **kwargs):
            captured["to"] = getattr(args, "to", None)
            captured["target"] = getattr(args, "target", None)
            captured["classification"] = getattr(args, "classification", None)
            captured["dispatch_decision"] = getattr(args, "dispatch_decision", None)
            captured["read_contract"] = getattr(args, "read_contract", None)
            captured["ticketless"] = kwargs.get("ticketless")
            return 0

        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
        cli_workflow, "_herdr_step_preflight", lambda _a: None
    ), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[gateway, grandparent]
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
        ), patch.object(
            commands, "orchestrate_handoff", side_effect=fake_orchestrate
        ), contextlib.redirect_stdout(out):
            rc = cli_workflow.cmd_workflow_step(_args(callback="blocked"))

        self.assertEqual(rc, 0)
        self.assertEqual(captured["to"], "codex")
        # The caller lane is resolved and passed as an explicit --target (no implicit
        # same-session codex fallback) — Redmine #12755 review j#67585.
        self.assertEqual(captured["target"], "%gp")
        self.assertEqual(captured["classification"], "blocked")
        self.assertEqual(captured["dispatch_decision"], "hand_back_to_caller")
        # The project gateway returns up to the grandparent coordinator.
        self.assertEqual(captured["read_contract"], "grandparent_coordinator")
        self.assertTrue(captured["ticketless"])
        self.assertIn("execution: executed", out.getvalue())

    def test_callback_caller_missing_fails_closed_without_dispatch(self):
        # No caller lane present: must fail closed, never fall back to a same-session
        # codex target.
        gateway = _cand("%self", project_scope=PROJECT)
        with patch.object(commands, "orchestrate_handoff") as orch:
            rc, text = _run(_args(callback="blocked"), [gateway])
        orch.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertIn("caller_missing", text)


class HerdrForwardLegCliTest(unittest.TestCase):
    """The Increment-3 herdr coordinator-forward leg is fired only on a non-dry-run step (#13583).

    Under the herdr backend the preflight resolves a coordinator lane to a ready forward outcome.
    ``cmd_workflow_step`` must dispatch the dedicated forward leg exactly once on execute, and NEVER
    on ``--dry-run`` (dry-run purity, safety-contract point 6): a dry-run resolves the route/result
    and touches no fence / send.
    """

    def _forward_outcome(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
            EXECUTION_READY,
            OWNER_PARENT,
            STATE_GRANDPARENT_CONSULTATION,
            WorkflowStepOutcome,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (
            PRIMITIVE_HERDR_FORWARD_CONSULT,
            REASON_HERDR_FORWARD_CONSULT_READY,
        )

        return WorkflowStepOutcome(
            state=STATE_GRANDPARENT_CONSULTATION,
            next_action="forward a single ticketless consultation to the single live gateway",
            execution=EXECUTION_READY,
            reason=REASON_HERDR_FORWARD_CONSULT_READY,
            next_owner=OWNER_PARENT,
            primitive=PRIMITIVE_HERDR_FORWARD_CONSULT,
            caller_role="grandparent_coordinator",
            repo_root=REPO,
            durable_anchor="none",
        )

    def _run_forward(self, args, *, leg_result):
        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: self._forward_outcome()
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=_ABSENT_STORE
        ), patch.object(
            cli_workflow, "_execute_herdr_forward_leg", return_value=leg_result
        ) as leg_mock, contextlib.redirect_stdout(out):
            rc = cli_workflow.cmd_workflow_step(args)
        return rc, out.getvalue(), leg_mock

    def test_dry_run_never_fires_the_forward_leg(self):
        rc, text, leg_mock = self._run_forward(_args(dry_run=True), leg_result=(0, ""))
        leg_mock.assert_not_called()  # dry-run: zero fence / send
        self.assertEqual(rc, 0)
        self.assertIn("execution: dry_run", text)
        self.assertIn("herdr_forward_consultation_ready", text)

    def test_execute_fires_the_forward_leg_once(self):
        rc, text, leg_mock = self._run_forward(
            _args(dry_run=False), leg_result=(0, "forward_result: sent")
        )
        self.assertEqual(leg_mock.call_count, 1)
        self.assertEqual(rc, 0)
        self.assertIn("execution: executed", text)

    def test_forward_leg_rc_is_surfaced(self):
        # A fence-unavailable / uncertain leg returns rc 1; the step surfaces the real outcome.
        rc, _text, leg_mock = self._run_forward(
            _args(dry_run=False), leg_result=(1, "forward_result: zero_send")
        )
        self.assertEqual(leg_mock.call_count, 1)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
