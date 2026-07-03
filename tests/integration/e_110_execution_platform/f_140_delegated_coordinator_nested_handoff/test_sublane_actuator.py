"""Sublane live-actuator use-case composition tests (Redmine #12973).

Drives :class:`SublaneActuateUseCase` against a fake :class:`SublaneActuatorOps` port (the
established #12604 / #12955 fake-port style), covering the fail-closed creation-side
actuation seam **without any real tmux / git / handoff side effect**:

- a dry-run resolves the plan and performs nothing;
- a live run creates (or adopts) the worktree, appends (or adopts) the cockpit column,
  confirms the stamps on read-back, and dispatches the gateway handoff — stopping at the
  first failure and reporting the partial state, never a partial success;
- every acceptance fail-closed trigger (missing identity, anchor-required, worktree /
  branch collision, pane-creation failure, stamp failure, handoff failure) blocks.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
    SublaneActuateUseCase,
    SublaneActuatorOps,
    format_actuate_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_GATEWAY_NOTIFIED,
    DISPATCH_SKIPPED,
    REASON_ANCHOR_REQUIRED,
    REASON_HANDOFF_FAILED,
    REASON_LANE_MISMATCH,
    REASON_MISSING_IDENTITY,
    REASON_PANE_CREATE_FAILED,
    REASON_STAMP_FAILED,
    REASON_WORK_UNIT_BLOCKED,
    REASON_WORKTREE_CREATE_FAILED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneCreateRequest,
    SublaneLaneView,
)


def _lane(*, gateway="%120", worker="%121", repo_root="/wt/12973"):
    return SublaneLaneView(
        workspace_id="ws",
        lane_id="l1",
        lane_label="issue_12973_x",
        issue="12973",
        branch="b",
        repo_root=repo_root,
        gateway_pane=gateway,
        worker_pane=worker,
        state="active",
    )


class FakeActuatorOps:
    """A scriptable :class:`SublaneActuatorOps` recording every call made to it."""

    def __init__(
        self,
        *,
        git=True,
        worktree_exists=False,
        create_error=None,
        append_error=None,
        lanes=None,
        dispatch_rc=0,
        dispatch_error=None,
    ):
        self._git = git
        self._we = worktree_exists
        self._create_error = create_error
        self._append_error = append_error
        # Consumed one per read_lane call (front to back); exhausted -> None.
        self._lane_seq = list(lanes) if lanes is not None else []
        self._dispatch_rc = dispatch_rc
        self._dispatch_error = dispatch_error
        self.calls = []

    def is_git_workspace(self):
        self.calls.append("is_git")
        return self._git

    def worktree_exists(self, branch):
        self.calls.append(("worktree_exists", branch))
        return self._we

    def create_worktree(self, *, branch, worktree_path):
        self.calls.append(("create_worktree", branch, worktree_path))
        if self._create_error is not None:
            raise self._create_error

    def append_lane_column(self, worktree_path):
        self.calls.append(("append_lane_column", worktree_path))
        if self._append_error is not None:
            raise self._append_error

    def read_lane(self, worktree_path):
        self.calls.append(("read_lane", worktree_path))
        if not self._lane_seq:
            return None
        return self._lane_seq.pop(0)

    def dispatch_implementation_request(self, **kwargs):
        self.calls.append(("dispatch", kwargs))
        if self._dispatch_error is not None:
            raise self._dispatch_error
        return self._dispatch_rc

    # -- call-inspection helpers --
    def _names(self):
        return [c[0] if isinstance(c, tuple) else c for c in self.calls]


def _req(**kw):
    base = dict(
        issue="12973",
        lane_label="issue_12973_x",
        branch="b",
        worktree_path="/wt/12973",
        journal="70159",
        upstream_coordinator="%2",
    )
    base.update(kw)
    return SublaneCreateRequest(**base)


class PortConformanceTests(unittest.TestCase):
    def test_fake_satisfies_protocol(self):
        self.assertIsInstance(FakeActuatorOps(), SublaneActuatorOps)


class DryRunTests(unittest.TestCase):
    def test_dry_run_performs_nothing(self):
        ops = FakeActuatorOps(git=True)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=False)
        self.assertEqual(outcome.status, ACTUATE_READY)
        self.assertFalse(outcome.execute)
        self.assertIsNone(outcome.gateway_pane)
        # Only read probes ran; no mutation.
        self.assertNotIn("create_worktree", ops._names())
        self.assertNotIn("append_lane_column", ops._names())
        self.assertNotIn("dispatch", ops._names())

    def test_dry_run_does_not_require_anchor(self):
        # No journal, but a dry-run must not fail closed on the anchor.
        outcome = SublaneActuateUseCase(FakeActuatorOps()).run(
            _req(journal=None), execute=False
        )
        self.assertEqual(outcome.status, ACTUATE_READY)


class MissingIdentityTests(unittest.TestCase):
    def test_missing_field_blocks_before_probe(self):
        ops = FakeActuatorOps(git=True)
        outcome = SublaneActuateUseCase(ops).run(_req(worktree_path=""), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_MISSING_IDENTITY, outcome.blocked_reasons)
        self.assertEqual(ops.calls, [])  # short-circuit before any probe

    def test_anchor_required_when_execute_dispatch_without_journal(self):
        outcome = SublaneActuateUseCase(FakeActuatorOps()).run(
            _req(journal=None), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_ANCHOR_REQUIRED, outcome.blocked_reasons)

    def test_no_dispatch_execute_without_journal_is_allowed(self):
        # --no-dispatch drops the anchor requirement (no worker is dispatched).
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(
            _req(journal=None), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_SKIPPED)


class WorkUnitGateTests(unittest.TestCase):
    """#13002: epic / feature units never actuate without an explicit decision."""

    def test_epic_without_decision_anchor_blocks_before_probe(self):
        ops = FakeActuatorOps(git=True)
        outcome = SublaneActuateUseCase(ops).run(
            _req(work_unit="epic"), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_WORK_UNIT_BLOCKED, outcome.blocked_reasons)
        self.assertIn(
            "work_unit_explicit_decision_required", outcome.blocked_reasons
        )
        self.assertEqual(ops.calls, [])  # short-circuit before any probe

    def test_feature_without_decision_anchor_blocks_dry_run_too(self):
        outcome = SublaneActuateUseCase(FakeActuatorOps()).run(
            _req(work_unit="feature"), execute=False
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_WORK_UNIT_BLOCKED, outcome.blocked_reasons)

    def test_epic_with_durable_decision_anchor_executes(self):
        ops = FakeActuatorOps(
            git=True, worktree_exists=False, lanes=[None, _lane()], dispatch_rc=0
        )
        outcome = SublaneActuateUseCase(ops).run(
            _req(work_unit="epic", work_unit_decision_anchor="70719"),
            execute=True,
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)

    def test_leaf_issue_exception_unit_executes(self):
        ops = FakeActuatorOps(
            git=True, worktree_exists=False, lanes=[None, _lane()], dispatch_rc=0
        )
        outcome = SublaneActuateUseCase(ops).run(
            _req(work_unit="leaf_issue"), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)


class ExecuteHappyPathTests(unittest.TestCase):
    def test_create_append_dispatch(self):
        ops = FakeActuatorOps(
            git=True, worktree_exists=False, lanes=[None, _lane()], dispatch_rc=0
        )
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertFalse(outcome.adopted)
        self.assertEqual(outcome.gateway_pane, "%120")
        self.assertEqual(outcome.worker_pane, "%121")
        self.assertEqual(outcome.dispatch_target, "%120")
        # #12986: a successful gateway send is `gateway_notified`, not `sent`, and
        # is NOT worker-confirmed — the gateway still owes a worker dispatch.
        self.assertEqual(outcome.dispatch_result, DISPATCH_GATEWAY_NOTIFIED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        names = ops._names()
        self.assertIn("create_worktree", names)
        self.assertIn("append_lane_column", names)
        self.assertIn("dispatch", names)

    def test_adopt_existing_lane_skips_append(self):
        ops = FakeActuatorOps(git=True, worktree_exists=True, lanes=[_lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.adopted)
        self.assertNotIn("append_lane_column", ops._names())
        # a reuse launch never calls create_worktree
        self.assertNotIn("create_worktree", ops._names())

    def test_non_git_skips_worktree_but_still_appends(self):
        ops = FakeActuatorOps(git=False, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.launch_action, "skip_no_git")
        self.assertNotIn("create_worktree", ops._names())
        self.assertIn("append_lane_column", ops._names())

    def test_no_dispatch_stops_after_panes(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True, dispatch=False)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_SKIPPED)
        self.assertIsNone(outcome.dispatch_target)
        self.assertNotIn("dispatch", ops._names())


class ExecuteFailClosedTests(unittest.TestCase):
    def test_worktree_create_failure_blocks(self):
        ops = FakeActuatorOps(
            git=True, worktree_exists=False, create_error=RuntimeError("path exists")
        )
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_WORKTREE_CREATE_FAILED, outcome.blocked_reasons)
        # never proceeded to append after the worktree failure
        self.assertNotIn("append_lane_column", ops._names())

    def test_append_failure_blocks(self):
        ops = FakeActuatorOps(
            git=True, lanes=[None], append_error=RuntimeError("split failed")
        )
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_PANE_CREATE_FAILED, outcome.blocked_reasons)
        self.assertNotIn("dispatch", ops._names())

    def test_panes_not_visible_on_readback_blocks(self):
        # Append returns, but read-back shows no worker pane.
        half = _lane(worker=None)
        ops = FakeActuatorOps(git=True, lanes=[None, half])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_PANE_CREATE_FAILED, outcome.blocked_reasons)

    def test_missing_stamp_blocks(self):
        # Panes visible but no repo-root stamp on read-back.
        no_stamp = _lane(repo_root=None)
        ops = FakeActuatorOps(git=True, lanes=[None, no_stamp])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_STAMP_FAILED, outcome.blocked_reasons)
        # panes were captured for the durable record even on the stamp block
        self.assertEqual(outcome.gateway_pane, "%120")

    def test_dispatch_failure_blocks_with_panes_recorded(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], dispatch_rc=1)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_HANDOFF_FAILED, outcome.blocked_reasons)
        # panes exist (created) but dispatch failed -> fail-closed, no partial ok
        self.assertEqual(outcome.gateway_pane, "%120")
        self.assertEqual(outcome.worker_pane, "%121")

    def test_dispatch_exception_blocks(self):
        ops = FakeActuatorOps(
            git=True, lanes=[None, _lane()], dispatch_error=RuntimeError("tmux gone")
        )
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_HANDOFF_FAILED, outcome.blocked_reasons)


def _wrong_lane(*, lane_label="issue_99999_wrong", issue="99999", workspace_id="other-ws",
                gateway="%999", worker="%998", repo_root="/wt/12973"):
    """A colliding lane: same repo_root as the request, but a different identity."""
    return SublaneLaneView(
        workspace_id=workspace_id,
        lane_id="lx",
        lane_label=lane_label,
        issue=issue,
        branch="z",
        repo_root=repo_root,
        gateway_pane=gateway,
        worker_pane=worker,
        state="active",
    )


class LaneIdentityValidationTests(unittest.TestCase):
    """Review j#70250: never adopt / dispatch to a lane whose identity mismatches."""

    def test_adopt_mismatched_lane_fails_closed(self):
        # A live lane shares the repo_root but carries a different issue / lane_label /
        # workspace — it must not be adopted or dispatched to (the reviewer's repro).
        ops = FakeActuatorOps(git=True, worktree_exists=True, lanes=[_wrong_lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_MISMATCH, outcome.blocked_reasons)
        self.assertFalse(outcome.adopted)
        # never appended onto the ambiguous target, never dispatched to %999
        self.assertNotIn("append_lane_column", ops._names())
        self.assertNotIn("dispatch", ops._names())

    def test_appended_lane_identity_mismatch_fails_closed(self):
        # No existing lane, so we create + append, but the read-back lane's stamped
        # identity does not match the request -> fail closed before dispatch.
        wrong = _wrong_lane(lane_label="issue_88888_other", issue="88888",
                            gateway="%777", worker="%776")
        ops = FakeActuatorOps(git=True, worktree_exists=False, lanes=[None, wrong])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_MISMATCH, outcome.blocked_reasons)
        self.assertNotIn("dispatch", ops._names())

    def test_issue_mismatch_against_matching_label_fails_closed(self):
        # Label matches but the requested issue disagrees with the lane's issue.
        ops = FakeActuatorOps(git=True, worktree_exists=True, lanes=[_lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(issue="88888"), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_MISMATCH, outcome.blocked_reasons)

    def test_matching_identity_still_adopts(self):
        # Sanity: the guard does not over-reject the correct lane.
        ops = FakeActuatorOps(git=True, worktree_exists=True, lanes=[_lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.adopted)


class RenderTests(unittest.TestCase):
    def test_text_render_marks_blocked(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], dispatch_rc=1)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        text = format_actuate_text(outcome)
        self.assertIn("blocked", text)
        self.assertIn("handoff_failed", text)

    def test_gateway_notified_text_warns_worker_unconfirmed(self):
        # #12986: the human-facing render must not read as full success; it flags
        # that only the gateway was notified and points at callback-recovery.
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], dispatch_rc=0)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        text = format_actuate_text(outcome)
        self.assertIn("gateway_notified", text)
        self.assertIn("worker dispatch NOT confirmed", text)
        self.assertIn("callback-recovery", text)
        # #12988: the render points at the ack drive that promotes the state.
        self.assertIn("sublane dispatch-worker --execute", text)
        # the executed reason itself carries the honest clause
        self.assertIn("worker dispatch NOT yet confirmed", outcome.reason)

    def test_payload_is_machine_readable(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        payload = outcome.as_payload()
        self.assertEqual(payload["gateway_pane"], "%120")
        self.assertEqual(payload["dispatch_result"], "gateway_notified")
        self.assertFalse(payload["worker_dispatch_confirmed"])
        self.assertEqual(payload["durable_anchor"], "70159")
        self.assertIsInstance(payload["steps"], list)


class LiveAppendLaneArgvTest(unittest.TestCase):
    """The #13155 launch-model threading into the live ``cockpit append`` argv.

    Exercises :meth:`LiveSublaneActuatorOps.append_lane_column` against a real
    worktree ``.mozyo-bridge/config.yaml``, patching ``_drive_cli`` to capture the
    argv it drives (no tmux / CLI execution).
    """

    def _argv_for(self, config_text):
        import tempfile
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            if config_text is not None:
                (wt / ".mozyo-bridge").mkdir()
                (wt / ".mozyo-bridge" / "config.yaml").write_text(
                    config_text, encoding="utf-8"
                )
            ops = LiveSublaneActuatorOps(repo_root=wt)
            captured = {}

            def _capture(argv):
                captured["argv"] = argv
                return 0

            # LiveSublaneActuatorOps is a frozen dataclass, so patch the class
            # attribute (MagicMock is not a descriptor -> called with just argv).
            with patch.object(LiveSublaneActuatorOps, "_drive_cli", side_effect=_capture):
                ops.append_lane_column(str(wt))
            return str(wt), captured["argv"]

    def test_no_config_is_historical_argv(self):
        wt, argv = self._argv_for(None)
        self.assertEqual(
            argv, ["cockpit", "append", "--repo", wt, "--no-attach"]
        )
        self.assertNotIn("--claude-model", argv)

    def test_config_without_model_is_historical_argv(self):
        wt, argv = self._argv_for("version: 1\n")
        self.assertEqual(
            argv, ["cockpit", "append", "--repo", wt, "--no-attach"]
        )

    def test_configured_model_appends_claude_model_flag(self):
        wt, argv = self._argv_for(
            "agent_launch:\n  sublane_claude_model: claude-opus-4-8\n"
        )
        self.assertEqual(
            argv,
            [
                "cockpit", "append", "--repo", wt, "--no-attach",
                "--claude-model", "claude-opus-4-8",
            ],
        )


if __name__ == "__main__":
    unittest.main()
