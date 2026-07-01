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
    DISPATCH_SENT,
    DISPATCH_SKIPPED,
    REASON_ANCHOR_REQUIRED,
    REASON_HANDOFF_FAILED,
    REASON_MISSING_IDENTITY,
    REASON_PANE_CREATE_FAILED,
    REASON_STAMP_FAILED,
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
        self.assertEqual(outcome.dispatch_result, DISPATCH_SENT)
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


class RenderTests(unittest.TestCase):
    def test_text_render_marks_blocked(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], dispatch_rc=1)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        text = format_actuate_text(outcome)
        self.assertIn("blocked", text)
        self.assertIn("handoff_failed", text)

    def test_payload_is_machine_readable(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        payload = outcome.as_payload()
        self.assertEqual(payload["gateway_pane"], "%120")
        self.assertEqual(payload["dispatch_result"], "sent")
        self.assertEqual(payload["durable_anchor"], "70159")
        self.assertIsInstance(payload["steps"], list)


if __name__ == "__main__":
    unittest.main()
