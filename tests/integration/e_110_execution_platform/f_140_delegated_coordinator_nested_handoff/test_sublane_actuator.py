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
    STEP_EXECUTED,
    STEP_READY,
    STEP_SKIPPED,
    ActuationStep,
    SublaneActuationOutcome,
    render_actuation_journal,
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
        append_argv=None,
        gateway_ready=True,
    ):
        self._git = git
        self._we = worktree_exists
        self._create_error = create_error
        self._append_error = append_error
        # #13155: scripted `cockpit append` argv the resolver would return; None ->
        # the historical argv for the worktree (no configured launch model).
        self._append_argv = append_argv
        # Consumed one per read_lane call (front to back); exhausted -> None.
        self._lane_seq = list(lanes) if lanes is not None else []
        self._dispatch_rc = dispatch_rc
        self._dispatch_error = dispatch_error
        # #13293: gateway readiness probe result. A bool -> that value on every probe;
        # a list/tuple -> consumed one per probe (front to back), last value sticky.
        self._gateway_ready_seq = (
            list(gateway_ready)
            if isinstance(gateway_ready, (list, tuple))
            else [gateway_ready]
        )
        self.calls = []

    def is_git_workspace(self):
        self.calls.append("is_git")
        return self._git

    def worktree_exists(self, branch):
        self.calls.append(("worktree_exists", branch))
        return self._we

    def create_worktree(self, *, branch, worktree_path, base_ref=None):
        # #13293: base_ref threaded through so the recorded call carries the pinned base.
        self.calls.append(("create_worktree", branch, worktree_path, base_ref))
        if self._create_error is not None:
            raise self._create_error

    def append_lane_column(self, worktree_path):
        self.calls.append(("append_lane_column", worktree_path))
        if self._append_error is not None:
            raise self._append_error

    def append_lane_argv(self, worktree_path):
        if self._append_argv is not None:
            return list(self._append_argv)
        return ["cockpit", "append", "--repo", worktree_path, "--no-attach"]

    def read_lane(self, worktree_path):
        self.calls.append(("read_lane", worktree_path))
        if not self._lane_seq:
            return None
        return self._lane_seq.pop(0)

    def probe_gateway_ready(self, gateway_pane):
        self.calls.append(("probe_gateway_ready", gateway_pane))
        # Consume one scripted value per probe; the final value is sticky.
        if len(self._gateway_ready_seq) > 1:
            return bool(self._gateway_ready_seq.pop(0))
        return bool(self._gateway_ready_seq[0]) if self._gateway_ready_seq else True

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

    def _append_step_command(self, ops):
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=False)
        step = next(s for s in outcome.steps if s.title == "append lane column")
        return step.command

    def test_dry_run_preview_reflects_configured_model(self):
        # #13155 REV2: the append-step preview must show the configured launch
        # model so an operator confirms the worker will stand up on it BEFORE run.
        ops = FakeActuatorOps(
            git=True,
            append_argv=[
                "cockpit", "append", "--repo", "/wt/12973", "--no-attach",
                "--claude-model", "claude-opus-4-8",
            ],
        )
        self.assertEqual(
            self._append_step_command(ops),
            "mozyo-bridge cockpit append --repo /wt/12973 --no-attach "
            "--claude-model claude-opus-4-8",
        )

    def test_dry_run_preview_without_model_is_historical(self):
        command = self._append_step_command(FakeActuatorOps(git=True))
        self.assertEqual(
            command, "mozyo-bridge cockpit append --repo /wt/12973 --no-attach"
        )
        self.assertNotIn("--claude-model", command)


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

    def test_planned_worktree_resolves_model_from_source_repo(self):
        # j#71880: the normal `sublane start --dry-run` previews a worktree that
        # does NOT exist yet. The launch model must come from the source repo's
        # config, so the preview still shows --claude-model before creation.
        import tempfile

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_append_argv import (  # noqa: E501
            resolve_append_lane_argv,
        )

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".mozyo-bridge").mkdir()
            (repo / ".mozyo-bridge" / "config.yaml").write_text(
                "agent_launch:\n  sublane_claude_model: claude-opus-4-8\n",
                encoding="utf-8",
            )
            planned = str(repo / "not-created-yet" / "lane")
            argv = resolve_append_lane_argv(planned, config_root=repo)
            self.assertEqual(
                ["cockpit", "append", "--repo", planned, "--no-attach",
                 "--claude-model", "claude-opus-4-8"],
                argv,
            )
            # Unconfigured source repo: historical argv, byte-for-byte.
            bare = Path(d) / "bare-repo"
            bare.mkdir()
            self.assertEqual(
                ["cockpit", "append", "--repo", planned, "--no-attach"],
                resolve_append_lane_argv(planned, config_root=bare),
            )

    def test_live_drive_and_preview_share_one_resolver(self):
        # #13155 REV2 (c): the live drive (`append_lane_column`) and the dry-run
        # preview source (`append_lane_argv`) resolve the SAME argv from the SAME
        # resolver, so what the operator previews is byte-for-byte what runs.
        import tempfile
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            LiveSublaneActuatorOps,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_append_argv import (  # noqa: E501
            resolve_append_lane_argv,
        )

        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / ".mozyo-bridge").mkdir()
            (wt / ".mozyo-bridge" / "config.yaml").write_text(
                "agent_launch:\n  sublane_claude_model: claude-opus-4-8\n",
                encoding="utf-8",
            )
            wt_s = str(wt / "planned-worktree")  # does not exist (j#71880)
            expected = resolve_append_lane_argv(wt_s, config_root=wt)
            self.assertIn("--claude-model", expected)
            ops = LiveSublaneActuatorOps(repo_root=wt)
            # Preview source: what `_dry_run` renders its command string from.
            self.assertEqual(ops.append_lane_argv(wt_s), expected)
            # Live drive: what `append_lane_column` actually drives.
            captured = {}

            def _capture(argv):
                captured["argv"] = argv
                return 0

            with patch.object(LiveSublaneActuatorOps, "_drive_cli", side_effect=_capture):
                ops.append_lane_column(wt_s)
            self.assertEqual(captured["argv"], expected)


def _step(outcome, title):
    return next(s for s in outcome.steps if s.title == title)


class BaseRefContractTests(unittest.TestCase):
    """#13293 evidence 1: --base-ref pins the worktree base (the j#72677 base trap)."""

    def _create_call(self, ops):
        return next(c for c in ops.calls if c[0] == "create_worktree")

    def test_base_ref_threaded_into_create_worktree(self):
        ops = FakeActuatorOps(git=True, worktree_exists=False, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(
            _req(base_ref="origin/main"), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        # the port received the pinned base positionally (branch, path, base_ref)
        _, branch, path, base = self._create_call(ops)
        self.assertEqual(base, "origin/main")
        # the executed step command replays the base as the git positional
        step = _step(outcome, "create worktree")
        self.assertEqual(
            step.command, "git worktree add /wt/12973 -b b origin/main"
        )
        self.assertIn("from base origin/main", step.detail)

    def test_no_base_ref_is_historical_command(self):
        ops = FakeActuatorOps(git=True, worktree_exists=False, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        _, branch, path, base = self._create_call(ops)
        self.assertIsNone(base)
        step = _step(outcome, "create worktree")
        self.assertEqual(step.command, "git worktree add /wt/12973 -b b")
        self.assertNotIn("from base", step.detail)

    def test_dry_run_preview_reflects_base_ref(self):
        outcome = SublaneActuateUseCase(FakeActuatorOps(git=True)).run(
            _req(base_ref="b2de4aa"), execute=False
        )
        step = _step(outcome, "create worktree")
        self.assertEqual(step.command, "git worktree add /wt/12973 -b b b2de4aa")

    def test_live_git_ops_append_base_positional(self):
        # #13293: the live git adapter appends the base as the <commit-ish> positional
        # so a stale checkout can never silently cut the lane from an unintended base.
        from unittest.mock import patch
        from subprocess import CompletedProcess

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E501
            LiveSublaneGitOperations,
        )

        git = LiveSublaneGitOperations(repo_root=Path("/repo"))
        seen = {}

        def _fake_run(*args):
            seen["args"] = args
            return CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(LiveSublaneGitOperations, "_run", side_effect=_fake_run):
            git.create_worktree(
                branch="b", worktree_path="/wt", base_ref="origin/main"
            )
        self.assertEqual(
            seen["args"], ("worktree", "add", "/wt", "-b", "b", "origin/main")
        )

    def test_live_git_ops_without_base_is_historical(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E501
            LiveSublaneGitOperations,
        )

        git = LiveSublaneGitOperations(repo_root=Path("/repo"))
        seen = {}

        def _fake_run(*args):
            seen["args"] = args
            return CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(LiveSublaneGitOperations, "_run", side_effect=_fake_run):
            git.create_worktree(branch="b", worktree_path="/wt")
        self.assertEqual(seen["args"], ("worktree", "add", "/wt", "-b", "b"))


class GatewayReadinessContractTests(unittest.TestCase):
    """#13293 evidence 3: the bounded, non-fatal pre-dispatch gateway readiness wait."""

    def test_ready_on_first_probe_dispatches_into_live_composer(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], gateway_ready=True)
        slept = []
        outcome = SublaneActuateUseCase(ops, sleep=slept.append).run(
            _req(), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.gateway_ready)
        # readiness confirmed before dispatch, and no back-off was needed
        self.assertEqual(slept, [])
        # the readiness step runs before dispatch (order 4 vs 5)
        readiness = _step(outcome, "confirm gateway readiness")
        dispatch = _step(outcome, "dispatch implementation_request")
        self.assertEqual(readiness.status, STEP_EXECUTED)
        self.assertLess(readiness.order, dispatch.order)
        # the probe was consulted with the resolved gateway pane
        self.assertIn(("probe_gateway_ready", "%120"), ops.calls)

    def test_unready_then_ready_backs_off_until_ready(self):
        # First two probes report not-ready, the third is ready -> two back-offs.
        ops = FakeActuatorOps(
            git=True, lanes=[None, _lane()], gateway_ready=[False, False, True]
        )
        slept = []
        outcome = SublaneActuateUseCase(
            ops, gateway_ready_probes=5, gateway_ready_interval_seconds=0.5,
            sleep=slept.append,
        ).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.gateway_ready)
        self.assertEqual(slept, [0.5, 0.5])

    def test_never_ready_degrades_but_still_dispatches(self):
        # The rail is never hard-blocked: an unconfirmed readiness dispatches anyway.
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], gateway_ready=False)
        slept = []
        outcome = SublaneActuateUseCase(
            ops, gateway_ready_probes=3, sleep=slept.append
        ).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)  # NOT blocked
        self.assertFalse(outcome.gateway_ready)
        self.assertEqual(outcome.dispatch_result, DISPATCH_GATEWAY_NOTIFIED)
        self.assertIn("dispatch", ops._names())  # dispatch still happened
        # probed the full window (3 probes, 2 back-offs) then degraded
        self.assertEqual(slept, [0.5, 0.5])
        readiness = _step(outcome, "confirm gateway readiness")
        self.assertEqual(readiness.status, STEP_SKIPPED)
        # the honest record warns the coordinator to watch for a no-progress lane
        self.assertIn("readiness NOT confirmed", outcome.reason)
        self.assertIn("- gateway_ready: false", render_actuation_journal(outcome))
        self.assertIn("gateway_ready: false", format_actuate_text(outcome))

    def test_disabled_wait_skips_probe_and_records_none(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops, gateway_ready_probes=0).run(
            _req(), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIsNone(outcome.gateway_ready)
        self.assertNotIn("probe_gateway_ready", ops._names())
        # a None readiness never renders a journal line (back-compat record)
        self.assertNotIn("gateway_ready", render_actuation_journal(outcome))

    def test_no_dispatch_never_probes_readiness(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(
            _req(), execute=True, dispatch=False
        )
        self.assertEqual(outcome.dispatch_result, DISPATCH_SKIPPED)
        self.assertIsNone(outcome.gateway_ready)
        self.assertNotIn("probe_gateway_ready", ops._names())
        self.assertEqual(_step(outcome, "confirm gateway readiness").status, STEP_SKIPPED)

    def test_dry_run_shows_readiness_step_without_probing(self):
        outcome = SublaneActuateUseCase(FakeActuatorOps(git=True)).run(
            _req(), execute=False
        )
        readiness = _step(outcome, "confirm gateway readiness")
        dispatch = _step(outcome, "dispatch implementation_request")
        self.assertEqual(readiness.status, STEP_READY)
        self.assertLess(readiness.order, dispatch.order)
        self.assertIsNone(outcome.gateway_ready)

    def test_dispatch_failure_still_records_readiness(self):
        ops = FakeActuatorOps(git=True, lanes=[None, _lane()], dispatch_rc=1)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_HANDOFF_FAILED, outcome.blocked_reasons)
        self.assertTrue(outcome.gateway_ready)  # readiness was observed before the send


class LiveGatewayReadyProbeTests(unittest.TestCase):
    """The live ``probe_gateway_ready`` composes real pane primitives, never fatal."""

    def _ops(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        return LiveSublaneActuatorOps(repo_root=Path("/repo"))

    def test_ready_when_agent_up_and_rendered(self):
        from unittest.mock import patch

        base = "mozyo_bridge.e_110_execution_platform"
        with patch(
            base + ".f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_info",
            return_value={"command": "codex"},
        ), patch(
            base + ".f_120_agent_discovery_pane_resolution.domain.pane_resolver."
            "is_receiver_agent_process",
            return_value=True,
        ), patch(
            base + ".f_130_handoff_routing.infrastructure.tmux_client.capture_pane",
            return_value="codex ready  context: 0%",
        ):
            self.assertTrue(self._ops().probe_gateway_ready("%14"))

    def test_not_ready_when_process_not_agent(self):
        from unittest.mock import patch

        base = "mozyo_bridge.e_110_execution_platform"
        with patch(
            base + ".f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_info",
            return_value={"command": "zsh"},
        ), patch(
            base + ".f_120_agent_discovery_pane_resolution.domain.pane_resolver."
            "is_receiver_agent_process",
            return_value=False,
        ):
            self.assertFalse(self._ops().probe_gateway_ready("%14"))

    def test_probe_never_fatal_on_systemexit(self):
        # pane_resolver.die() raises SystemExit when a pane disappears; the probe must
        # swallow it and report not-ready, never crash the actuation.
        from unittest.mock import patch

        base = "mozyo_bridge.e_110_execution_platform"
        with patch(
            base + ".f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_info",
            side_effect=SystemExit(1),
        ):
            self.assertFalse(self._ops().probe_gateway_ready("%14"))


class JsonEnvelopeContractTests(unittest.TestCase):
    """#13293 evidence 2: --json confines inner CLI progress text off stdout."""

    def _drive_with_quiet(self, quiet):
        import io
        import contextlib as _ctx
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        ops = LiveSublaneActuatorOps(repo_root=Path("/repo"), quiet_stdout=quiet)

        class _Args:
            def func(self, _a):
                print("INNER-DELIVERY-PROGRESS")
                return 0

        fake_args = _Args()

        out, err = io.StringIO(), io.StringIO()
        # Patch the composed CLI parse so _drive_cli runs our printing stub handler.
        with patch(
            "mozyo_bridge.application.cli.build_parser"
        ) as bp, patch(
            "mozyo_bridge.application.cli.normalize_paths", side_effect=lambda a: a
        ):
            bp.return_value.parse_args.return_value = fake_args
            fake_args.func = lambda a: (print("INNER-DELIVERY-PROGRESS"), 0)[1]
            with _ctx.redirect_stdout(out), _ctx.redirect_stderr(err):
                rc = ops._drive_cli(["handoff", "send"])
        return rc, out.getvalue(), err.getvalue()

    def test_quiet_routes_inner_text_to_stderr(self):
        rc, out, err = self._drive_with_quiet(quiet=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")  # stdout stays a clean channel for the JSON envelope
        self.assertIn("INNER-DELIVERY-PROGRESS", err)

    def test_non_quiet_keeps_inner_text_on_stdout(self):
        rc, out, err = self._drive_with_quiet(quiet=False)
        self.assertEqual(rc, 0)
        self.assertIn("INNER-DELIVERY-PROGRESS", out)


class ActuateTextPathRedactionTests(unittest.TestCase):
    """Redmine #13368: ``format_actuate_text`` carries no host-local absolute path."""

    _WT = "/workspace/parent/mozyo_bridge_issue_13368_record_path_redaction"
    _LABEL = "mozyo_bridge_issue_13368_record_path_redaction"

    def test_actuate_text_redacts_worktree_line_and_step_command(self):
        outcome = SublaneActuationOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="ok",
            issue="13368",
            lane_label="issue_13368_record_path_redaction",
            branch="issue_13368_record_path_redaction",
            worktree_path=self._WT,
            launch_action="create_worktree",
            gateway_pane="%1",
            worker_pane="%2",
            steps=(
                ActuationStep(
                    order=1,
                    title="create worktree",
                    status=STEP_EXECUTED,
                    detail="created",
                    command=f"git worktree add {self._WT} -b issue_13368_record_path_redaction",
                ),
            ),
        )
        text = format_actuate_text(outcome)
        # Neither the durable-record `- worktree:` line nor the `$ git worktree add`
        # command preview carries the absolute path.
        self.assertNotIn(self._WT, text)
        self.assertIn(f"- worktree: {self._LABEL}", text)
        self.assertIn(f"git worktree add {self._LABEL}", text)
        # The machine payload keeps the absolute path (local surface).
        self.assertEqual(outcome.as_payload()["worktree_path"], self._WT)


if __name__ == "__main__":
    unittest.main()
