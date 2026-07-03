"""Worker-dispatch ack-drive use-case composition tests (Redmine #12988).

Drives :class:`WorkerDispatchUseCase` against a fake :class:`WorkerDispatchOps`
port (the established #12973 fake-port style), covering the fail-closed drive
seam **without any real tmux / handoff side effect**:

- a dry-run resolves the transfer (lane, worker pane, replayable command) and
  performs nothing;
- a live run drives the same-lane send and promotes to ``worker_dispatched`` /
  ``worker_dispatch_confirmed=true`` **only** on a measured delivery ACK
  (send exit 0);
- every fail-closed trigger blocks before / instead of a promotion: missing
  identity, missing durable anchor, unresolved lane, lane-identity mismatch
  (j#70250 guard), missing worker / gateway pane, and a failed or raising send
  (recorded as ``delivery_failed``, keeping ``gateway_notified`` semantics);
- the CLI wiring exposes ``sublane dispatch-worker`` with the drive handler.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    LiveWorkerDispatchOps,
    WorkerDispatchOps,
    WorkerDispatchUseCase,
    cmd_sublane_dispatch_worker,
    format_worker_dispatch_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_NOT_ATTEMPTED,
    DISPATCH_WORKER_DISPATCHED,
    REASON_ANCHOR_REQUIRED,
    REASON_LANE_MISMATCH,
    REASON_MISSING_IDENTITY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    REASON_LANE_NOT_RESOLVED,
    REASON_LANE_PANE_MISSING,
    REASON_WORKER_DISPATCH_FAILED,
    WORKER_DISPATCH_DELIVERY_FAILED,
    WorkerDispatchRequest,
)


def _lane(*, gateway="%176", worker="%177", label="issue_12988_x", issue="12988"):
    return SublaneLaneView(
        workspace_id="ws",
        lane_id="l1",
        lane_label=label,
        issue=issue,
        branch="b",
        repo_root="/wt/12988",
        gateway_pane=gateway,
        worker_pane=worker,
        state="active",
    )


class FakeWorkerDispatchOps:
    """A scriptable :class:`WorkerDispatchOps` recording every call made to it."""

    def __init__(self, *, lane=None, dispatch_rc=0, dispatch_error=None):
        self._lane = lane
        self._dispatch_rc = dispatch_rc
        self._dispatch_error = dispatch_error
        self.calls = []

    def read_lane(self, worktree_path):
        self.calls.append(("read_lane", worktree_path))
        return self._lane

    def dispatch_to_worker(self, **kwargs):
        self.calls.append(("dispatch", kwargs))
        if self._dispatch_error is not None:
            raise self._dispatch_error
        return self._dispatch_rc

    def _names(self):
        return [c[0] for c in self.calls]


def _req(**kw):
    base = dict(
        issue="12988",
        lane_label="issue_12988_x",
        worktree_path="/wt/12988",
        journal="71524",
    )
    base.update(kw)
    return WorkerDispatchRequest(**base)


class PortConformanceTests(unittest.TestCase):
    def test_fake_satisfies_protocol(self):
        self.assertIsInstance(FakeWorkerDispatchOps(), WorkerDispatchOps)


class DryRunTests(unittest.TestCase):
    def test_dry_run_resolves_transfer_and_sends_nothing(self):
        ops = FakeWorkerDispatchOps(lane=_lane())
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=False)
        self.assertEqual(outcome.status, ACTUATE_READY)
        self.assertFalse(outcome.execute)
        self.assertEqual(outcome.dispatch_result, DISPATCH_NOT_ATTEMPTED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertEqual(outcome.worker_pane, "%177")
        self.assertEqual(outcome.gateway_pane, "%176")
        self.assertEqual(outcome.dispatch_target, "%177")
        # The replayable command is the exact same-lane forward the gateway
        # would type, carrying the anchor + worker target + callback address.
        self.assertIn("handoff send --to claude", outcome.command)
        self.assertIn("--target %177", outcome.command)
        self.assertIn("--journal 71524", outcome.command)
        self.assertIn("gateway_callback_target=%176", outcome.command)
        self.assertNotIn("dispatch", ops._names())


class ExecuteTests(unittest.TestCase):
    def test_delivery_ack_promotes_to_worker_dispatched(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=0)
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_WORKER_DISPATCHED)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        # The confirmed reason stays a delivery ACK, never progress/completion.
        self.assertIn("delivery ACK only", outcome.reason)
        dispatched = dict(ops.calls)["dispatch"]
        self.assertEqual(dispatched["worker_pane"], "%177")
        self.assertEqual(dispatched["gateway_callback_target"], "%176")
        self.assertEqual(dispatched["issue"], "12988")
        self.assertEqual(dispatched["journal"], "71524")
        self.assertEqual(dispatched["lane_label"], "issue_12988_x")
        self.assertEqual(dispatched["target_repo"], "auto")

    def test_failed_send_is_delivery_failed_not_promoted(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=1)
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertEqual(outcome.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertIn(REASON_WORKER_DISPATCH_FAILED, outcome.blocked_reasons)
        self.assertIn("gateway_notified", outcome.reason)

    def test_raising_send_is_delivery_failed_not_promoted(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_error=RuntimeError("boom"))
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertEqual(outcome.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertIn("boom", outcome.reason)

    def test_system_exit_from_send_is_delivery_failed_not_a_process_exit(self):
        # Review j#71597 finding 1: the composed handoff CLI fails closed via
        # `die()` == SystemExit, which `except Exception` never catches. A port
        # that leaks it must still yield the fail-closed `delivery_failed`
        # outcome — never escape the use case and skip the durable record.
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_error=SystemExit(2))
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertEqual(outcome.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED)
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertIn(REASON_WORKER_DISPATCH_FAILED, outcome.blocked_reasons)
        self.assertIn("SystemExit(2)", outcome.reason)
        self.assertIn("gateway_notified", outcome.reason)

    def test_system_exit_zero_never_promotes(self):
        # An ambiguous exit (SystemExit with code 0 / None) is not a measured
        # delivery ACK; it must stay fail-closed, never `worker_dispatched`.
        for code in (0, None):
            ops = FakeWorkerDispatchOps(
                lane=_lane(), dispatch_error=SystemExit(code)
            )
            outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
            self.assertEqual(outcome.status, ACTUATE_BLOCKED)
            self.assertEqual(
                outcome.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED
            )
            self.assertFalse(outcome.worker_dispatch_confirmed)


class FailClosedTests(unittest.TestCase):
    def test_missing_identity_blocks_before_any_probe(self):
        ops = FakeWorkerDispatchOps(lane=_lane())
        outcome = WorkerDispatchUseCase(ops).run(
            _req(issue="", lane_label=""), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_MISSING_IDENTITY, outcome.blocked_reasons)
        self.assertIn("missing_field:issue", outcome.blocked_reasons)
        self.assertEqual(ops.calls, [])

    def test_live_send_requires_durable_anchor(self):
        ops = FakeWorkerDispatchOps(lane=_lane())
        outcome = WorkerDispatchUseCase(ops).run(_req(journal=None), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_ANCHOR_REQUIRED, outcome.blocked_reasons)
        self.assertEqual(ops.calls, [])

    def test_dry_run_without_anchor_is_allowed(self):
        ops = FakeWorkerDispatchOps(lane=_lane())
        outcome = WorkerDispatchUseCase(ops).run(_req(journal=None), execute=False)
        self.assertEqual(outcome.status, ACTUATE_READY)

    def test_unresolved_lane_blocks(self):
        ops = FakeWorkerDispatchOps(lane=None)
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_NOT_RESOLVED, outcome.blocked_reasons)
        self.assertNotIn("dispatch", ops._names())

    def test_lane_identity_mismatch_blocks_before_send(self):
        # j#70250 guard: never forward #<issue> to a different / stale lane.
        ops = FakeWorkerDispatchOps(lane=_lane(label="issue_9999_other", issue="9999"))
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_MISMATCH, outcome.blocked_reasons)
        self.assertNotIn("dispatch", ops._names())

    def test_missing_worker_pane_blocks(self):
        ops = FakeWorkerDispatchOps(lane=_lane(worker=None))
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_PANE_MISSING, outcome.blocked_reasons)
        self.assertIn("worker", outcome.reason)
        self.assertNotIn("dispatch", ops._names())

    def test_missing_gateway_pane_blocks(self):
        # The gateway pane is the worker's recorded same-lane callback address;
        # a transfer the worker cannot call back on fails closed.
        ops = FakeWorkerDispatchOps(lane=_lane(gateway=None))
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LANE_PANE_MISSING, outcome.blocked_reasons)
        self.assertIn("gateway", outcome.reason)
        self.assertNotIn("dispatch", ops._names())


class LiveOpsInnerCliContainmentTests(unittest.TestCase):
    """The live adapter contains the inner CLI's SystemExit + stdout (j#71597).

    The inner `handoff send` fails closed through `die()` == SystemExit and
    prints its own delivery record to stdout. The adapter must convert the
    exit to a plain rc (so the use case's fail-closed conversion always runs)
    and keep the outer stdout clean (so `--execute --json` stays
    machine-readable), surfacing the captured inner record on stderr only
    when the send failed.
    """

    def _dispatch(self, fake_func):
        ops = LiveWorkerDispatchOps(repo_root=Path("/wt/12988"))

        class FakeParser:
            def parse_args(self, argv):
                return Namespace(func=fake_func)

        out, err = io.StringIO(), io.StringIO()
        with patch(
            "mozyo_bridge.application.cli.build_parser",
            return_value=FakeParser(),
        ), patch(
            "mozyo_bridge.application.cli.normalize_paths",
            side_effect=lambda a: a,
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ops.dispatch_to_worker(
                issue="12988",
                journal="71579",
                worker_pane="%177",
                lane_label="issue_12988_x",
                gateway_callback_target="%176",
                target_repo="auto",
            )
        return rc, out.getvalue(), err.getvalue()

    def test_die_style_system_exit_becomes_rc_and_stdout_stays_clean(self):
        def fake_func(args):
            print("inner delivery record body")
            raise SystemExit(2)

        rc, out, err = self._dispatch(fake_func)
        self.assertEqual(rc, 2)
        self.assertNotIn("inner delivery record body", out)
        self.assertIn("inner delivery record body", err)

    def test_ambiguous_system_exit_is_a_failure_rc(self):
        for code in (0, None):
            rc, out, _err = self._dispatch(
                lambda args, code=code: (_ for _ in ()).throw(SystemExit(code))
            )
            self.assertEqual(rc, 1)
            self.assertEqual(out, "")

    def test_successful_send_keeps_stdout_clean_and_stderr_quiet(self):
        def fake_func(args):
            print("inner delivery record body")
            return 0

        rc, out, err = self._dispatch(fake_func)
        self.assertEqual(rc, 0)
        self.assertNotIn("inner delivery record body", out)
        self.assertEqual(err, "")

    def test_nonzero_return_surfaces_inner_record_on_stderr(self):
        def fake_func(args):
            print("inner delivery record body")
            return 1

        rc, out, err = self._dispatch(fake_func)
        self.assertEqual(rc, 1)
        self.assertNotIn("inner delivery record body", out)
        self.assertIn("inner delivery record body", err)


class RenderAndCliTests(unittest.TestCase):
    def test_text_render_carries_confirmed_state_and_record(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=0)
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        text = format_worker_dispatch_text(outcome)
        self.assertIn("sublane dispatch-worker: executed", text)
        self.assertIn("worker_dispatch_confirmed=true", text)
        self.assertIn("## sublane worker dispatched", text)

    def test_text_render_marks_delivery_failure(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=1)
        outcome = WorkerDispatchUseCase(ops).run(_req(), execute=True)
        text = format_worker_dispatch_text(outcome)
        self.assertIn("blocked: worker_dispatch_failed", text)
        self.assertIn("worker_dispatch_confirmed=false", text)
        self.assertIn("gateway_notified", text)

    def test_cli_parser_wires_dispatch_worker(self):
        from mozyo_bridge.application.cli import build_parser

        args = build_parser().parse_args(
            [
                "sublane",
                "dispatch-worker",
                "--issue",
                "12988",
                "--lane-label",
                "issue_12988_x",
                "--journal",
                "71524",
                "--repo",
                "/wt/12988",
            ]
        )
        self.assertIs(args.func, cmd_sublane_dispatch_worker)
        self.assertEqual(args.issue, "12988")
        self.assertEqual(args.lane_label, "issue_12988_x")
        self.assertEqual(args.journal, "71524")
        self.assertFalse(args.execute)
        self.assertEqual(args.target_repo, "auto")


if __name__ == "__main__":
    unittest.main()
