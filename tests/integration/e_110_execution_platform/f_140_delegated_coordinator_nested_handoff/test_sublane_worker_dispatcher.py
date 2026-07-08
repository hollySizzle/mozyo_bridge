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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    render_worker_dispatch_journal,
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
    WorkerDispatchOutcome,
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
    """A scriptable :class:`WorkerDispatchOps` recording every call made to it.

    ``worker_ready`` is the constant readiness the #13301 probe reports; pass
    ``worker_ready_sequence`` (a list of bools) to script a still-booting worker
    that becomes ready after N unconfirmed probes.
    """

    def __init__(
        self,
        *,
        lane=None,
        dispatch_rc=0,
        dispatch_error=None,
        worker_ready=True,
        worker_ready_sequence=None,
    ):
        self._lane = lane
        self._dispatch_rc = dispatch_rc
        self._dispatch_error = dispatch_error
        self._worker_ready = worker_ready
        self._worker_ready_sequence = (
            list(worker_ready_sequence)
            if worker_ready_sequence is not None
            else None
        )
        self.calls = []

    def read_lane(self, worktree_path):
        self.calls.append(("read_lane", worktree_path))
        return self._lane

    def probe_worker_ready(self, worker_pane):
        self.calls.append(("probe_worker_ready", worker_pane))
        if self._worker_ready_sequence:
            return self._worker_ready_sequence.pop(0)
        return self._worker_ready

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


class WorkerReadinessWaitTests(unittest.TestCase):
    """#13301: bounded, non-fatal pre-forward worker readiness wait.

    Mirrors the #13293 gateway readiness wait on the worker (Claude) pane: the
    execute drive polls the worker pane until it is booted before the queue-enter
    forward, but NEVER hard-blocks — an unconfirmed readiness records
    ``worker_ready=false`` and forwards anyway.
    """

    def _use_case(self, ops, *, probes=20, sleeps=None):
        return WorkerDispatchUseCase(
            ops,
            worker_ready_probes=probes,
            worker_ready_interval_seconds=0.0,
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        )

    def test_ready_worker_records_true_and_forwards(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), worker_ready=True)
        sleeps = []
        outcome = self._use_case(ops, sleeps=sleeps).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.worker_ready)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        # First probe was ready -> no wait slept, and the forward still happened.
        self.assertEqual(sleeps, [])
        self.assertIn("dispatch", ops._names())
        # The probe targets the resolved worker pane, before the send.
        self.assertEqual(("probe_worker_ready", "%177"), ops.calls[1])

    def test_unconfirmed_readiness_degrades_but_still_forwards(self):
        # A worker that never reports ready must NOT hard-block: worker_ready=false
        # is recorded and the forward proceeds (the queue-enter Enter-only retry is
        # the landing safety net).
        ops = FakeWorkerDispatchOps(lane=_lane(), worker_ready=False)
        sleeps = []
        outcome = self._use_case(ops, probes=3, sleeps=sleeps).run(
            _req(), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertFalse(outcome.worker_ready)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        # Bounded: 3 probes, 2 inter-probe sleeps (no trailing sleep).
        self.assertEqual(ops._names().count("probe_worker_ready"), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertIn("dispatch", ops._names())

    def test_still_booting_worker_becomes_ready_within_window(self):
        ops = FakeWorkerDispatchOps(
            lane=_lane(), worker_ready_sequence=[False, False, True]
        )
        sleeps = []
        outcome = self._use_case(ops, probes=20, sleeps=sleeps).run(
            _req(), execute=True
        )
        self.assertTrue(outcome.worker_ready)
        # Stopped polling on the first ready observation (3rd probe), 2 sleeps.
        self.assertEqual(ops._names().count("probe_worker_ready"), 3)
        self.assertEqual(len(sleeps), 2)

    def test_probes_zero_disables_wait_and_leaves_worker_ready_none(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), worker_ready=True)
        outcome = self._use_case(ops, probes=0).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIsNone(outcome.worker_ready)
        self.assertNotIn("probe_worker_ready", ops._names())
        self.assertIn("dispatch", ops._names())

    def test_dry_run_never_probes_worker_readiness(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), worker_ready=True)
        outcome = self._use_case(ops).run(_req(), execute=False)
        self.assertEqual(outcome.status, ACTUATE_READY)
        self.assertIsNone(outcome.worker_ready)
        self.assertNotIn("probe_worker_ready", ops._names())


class RouteGateExceptionPassthroughTests(unittest.TestCase):
    """#13301: --allow-direct-worker threads the #12918 route exception through.

    A drive from a pane whose lane Unit differs from the worker's (e.g. a
    coordinator stall-drive) otherwise fails closed inside the inner ``handoff
    send`` with ``gateway_route_blocked``. The flag threads the explicit durable
    exception onto the send so the cross-lane delivery is admitted and recorded
    distinctly, never silently.
    """

    def test_flag_threads_into_send_and_is_recorded(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=0)
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=0).run(
            _req(), execute=True, allow_direct_worker=True
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.allow_direct_worker)
        dispatched = dict(ops.calls)["dispatch"]
        self.assertTrue(dispatched["allow_direct_worker"])
        # The replayable command carries the explicit exception flag.
        self.assertIn("--allow-direct-worker", outcome.command)
        # The durable record spells the exception out distinctly.
        journal = render_worker_dispatch_journal(outcome)
        self.assertIn("route_exception: --allow-direct-worker", journal)
        self.assertIn("gateway_route_exception", journal)

    def test_default_omits_flag_backcompat(self):
        ops = FakeWorkerDispatchOps(lane=_lane(), dispatch_rc=0)
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=0).run(
            _req(), execute=True
        )
        self.assertFalse(outcome.allow_direct_worker)
        dispatched = dict(ops.calls)["dispatch"]
        self.assertFalse(dispatched["allow_direct_worker"])
        self.assertNotIn("--allow-direct-worker", outcome.command)
        self.assertNotIn("route_exception", render_worker_dispatch_journal(outcome))

    def test_dry_run_previews_flag_in_command_without_sending(self):
        ops = FakeWorkerDispatchOps(lane=_lane())
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=0).run(
            _req(), execute=False, allow_direct_worker=True
        )
        self.assertEqual(outcome.status, ACTUATE_READY)
        self.assertTrue(outcome.allow_direct_worker)
        self.assertIn("--allow-direct-worker", outcome.command)
        self.assertNotIn("dispatch", ops._names())


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
        # #13301 defaults: readiness wait on (10s), route exception off.
        self.assertEqual(args.worker_ready_timeout, 10.0)
        self.assertFalse(args.allow_direct_worker)

    def test_cli_parser_wires_worker_ready_timeout_and_allow_direct_worker(self):
        from mozyo_bridge.application.cli import build_parser

        args = build_parser().parse_args(
            [
                "sublane",
                "dispatch-worker",
                "--issue",
                "13301",
                "--lane-label",
                "issue_13301_x",
                "--worker-ready-timeout",
                "0",
                "--allow-direct-worker",
            ]
        )
        self.assertEqual(args.worker_ready_timeout, 0.0)
        self.assertTrue(args.allow_direct_worker)


class WorkerDispatchTextPathRedactionTests(unittest.TestCase):
    """Redmine #13368: ``format_worker_dispatch_text`` leaks no host-local abs path.

    The same-lane send uses ``--target-repo auto`` so the command carries no path;
    this pins that even with an absolute ``worktree_path`` on the outcome, the
    pasteable text stays path-free (defence-in-depth) while the machine payload
    keeps the absolute path.
    """

    _WT = "/workspace/parent/mozyo_bridge_issue_13368_record_path_redaction"

    def test_worker_dispatch_text_has_no_abs_worktree_path(self):
        outcome = WorkerDispatchOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="delivery-acked",
            issue="13368",
            lane_label="issue_13368_record_path_redaction",
            worktree_path=self._WT,
            gateway_pane="%1",
            worker_pane="%2",
            dispatch_target="%2",
            dispatch_result=DISPATCH_WORKER_DISPATCHED,
            durable_anchor="73502",
            command="mozyo-bridge handoff send --to claude --target-repo auto",
        )
        text = format_worker_dispatch_text(outcome)
        self.assertNotIn(self._WT, text)
        self.assertEqual(outcome.as_payload()["worktree_path"], self._WT)


if __name__ == "__main__":
    unittest.main()
