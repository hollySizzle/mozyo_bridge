"""Dispatch admission gate wiring tests (Redmine #13290).

Pins the fail-closed / explicit-override gate on the *live dispatch path* for both
actuators, driven against fake ports (no real tmux / git / handoff side effect):

- ``sublane create --execute`` (:class:`SublaneActuateUseCase`) and
- ``sublane dispatch-worker --execute`` (:class:`WorkerDispatchUseCase`)

For each: a caller-supplied ``stop_*`` fill decision fails the live dispatch closed
(before any side effect) unless an explicit override reason is supplied; a
``dispatch_next`` decision proceeds; and no fill context leaves the #12973 / #12988
contract byte-for-byte unchanged. The override is recorded on the outcome and in the
durable journal (reason + the anchor already carried).
"""
from __future__ import annotations

import sys
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
    SublaneActuateUseCase,
    resolve_dispatch_admission_args,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    WorkerDispatchUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_WORKER_DISPATCHED,
    REASON_FILL_STOP,
    render_actuation_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_dispatch_admission import (  # noqa: E501
    FILL_GATE_STOP_OVERRIDDEN,
    evaluate_dispatch_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneCreateRequest,
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    WorkerDispatchRequest,
    render_worker_dispatch_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (  # noqa: E501
    FILL_STOP_COORDINATOR_BLOCKING,
    FillDecisionInputs,
    LaneState,
)

ISSUE = "13290"
LABEL = "issue_13290_x"


def _lane():
    return SublaneLaneView(
        workspace_id="ws",
        lane_id="l1",
        lane_label=LABEL,
        issue=ISSUE,
        branch="b",
        repo_root="/wt/13290",
        gateway_pane="%290",
        worker_pane="%291",
        state="active",
    )


# A caller-supplied lane set that resolves to a real coordinator-blocking stop.
def _stop_inputs():
    return FillDecisionInputs(
        lanes=(LaneState(issue="13285", state_class="owner_waiting"),),
        ready_independent_work=3,
        capacity_remaining=3,
    )


def _dispatch_next_inputs():
    return FillDecisionInputs(
        lanes=(LaneState(issue="13285", state_class="implementing"),),
        ready_independent_work=3,
        capacity_remaining=3,
    )


# ---------------------------------------------------------------------------
# sublane create --execute (creation-side actuator).
# ---------------------------------------------------------------------------


class FakeActuatorOps:
    """Adopt-path fake: an already-live, identity-matching lane, dispatch exit 0."""

    def __init__(self):
        self.calls = []

    def is_git_workspace(self):
        return True

    def worktree_exists(self, branch):
        return True  # reuse; no create_worktree

    def create_worktree(self, *, branch, worktree_path):
        self.calls.append("create_worktree")

    def append_lane_column(self, worktree_path):
        self.calls.append("append_lane_column")

    def append_lane_argv(self, worktree_path):
        return ["cockpit", "append", "--repo", worktree_path, "--no-attach"]

    def read_lane(self, worktree_path):
        return _lane()

    def dispatch_implementation_request(self, **kwargs):
        self.calls.append("dispatch")
        return 0


def _create_req(**kw):
    base = dict(
        issue=ISSUE,
        lane_label=LABEL,
        branch="b",
        worktree_path="/wt/13290",
        journal="72669",
        upstream_coordinator="%2",
    )
    base.update(kw)
    return SublaneCreateRequest(**base)


class CreateGateTests(unittest.TestCase):
    def test_stop_without_override_fails_closed_before_side_effects(self):
        ops = FakeActuatorOps()
        outcome = SublaneActuateUseCase(ops).run(
            _create_req(), execute=True, fill_inputs=_stop_inputs()
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_FILL_STOP, outcome.blocked_reasons)
        self.assertIn(FILL_STOP_COORDINATOR_BLOCKING, outcome.blocked_reasons)
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        # Fail-closed BEFORE any worktree / append / dispatch side effect.
        self.assertEqual(ops.calls, [])

    def test_stop_with_override_proceeds_and_records(self):
        ops = FakeActuatorOps()
        outcome = SublaneActuateUseCase(ops).run(
            _create_req(),
            execute=True,
            fill_inputs=_stop_inputs(),
            override_fill_stop="owner intent #13229 j#72635",
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIn("dispatch", ops.calls)
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(
            outcome.fill_override_reason, "owner intent #13229 j#72635"
        )
        self.assertIn("fill-decision stop overridden", outcome.reason)
        # The override is recorded in the durable journal (reason + anchor).
        journal = render_actuation_journal(outcome)
        self.assertIn(f"- fill_decision: {FILL_STOP_COORDINATOR_BLOCKING}", journal)
        self.assertIn(
            "- fill_stop_override: owner intent #13229 j#72635", journal
        )
        self.assertIn("- durable_anchor: 72669", journal)

    def test_dispatch_next_proceeds(self):
        ops = FakeActuatorOps()
        outcome = SublaneActuateUseCase(ops).run(
            _create_req(), execute=True, fill_inputs=_dispatch_next_inputs()
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIn("dispatch", ops.calls)
        self.assertIsNone(outcome.fill_override_reason)

    def test_no_fill_context_is_back_compat(self):
        ops = FakeActuatorOps()
        outcome = SublaneActuateUseCase(ops).run(_create_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        # Gate not armed: no fill fields, and the journal stays unchanged.
        self.assertIsNone(outcome.fill_decision)
        self.assertNotIn("fill_decision", render_actuation_journal(outcome))

    def test_dry_run_is_never_gated(self):
        # A dry-run performs nothing, so a stop decision must not block it.
        ops = FakeActuatorOps()
        outcome = SublaneActuateUseCase(ops).run(
            _create_req(), execute=False, fill_inputs=_stop_inputs()
        )
        self.assertEqual(outcome.status, ACTUATE_READY)
        self.assertIsNone(outcome.fill_decision)

    def test_override_survives_a_later_execution_failure(self):
        # An override that passes the gate but then hits a real actuation failure
        # must still carry the override record: the durable journal has to stay
        # honest that a fill stop was intentionally overridden.
        class MismatchedLaneOps(FakeActuatorOps):
            def read_lane(self, worktree_path):
                return SublaneLaneView(
                    workspace_id="ws",
                    lane_id="l1",
                    lane_label="issue_99999_other",
                    issue="99999",
                    branch="b",
                    repo_root="/wt/13290",
                    gateway_pane="%290",
                    worker_pane="%291",
                    state="active",
                )

        ops = MismatchedLaneOps()
        outcome = SublaneActuateUseCase(ops).run(
            _create_req(),
            execute=True,
            fill_inputs=_stop_inputs(),
            override_fill_stop="owner intent #13229 j#72635",
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        # Blocked for the real actuation reason, not the (overridden) fill stop.
        self.assertNotIn(REASON_FILL_STOP, outcome.blocked_reasons)
        self.assertEqual(ops.calls, [])  # never dispatched
        # ...yet the override record survives into the durable journal.
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(
            outcome.fill_override_reason, "owner intent #13229 j#72635"
        )
        journal = render_actuation_journal(outcome)
        self.assertIn(
            "- fill_stop_override: owner intent #13229 j#72635", journal
        )


# ---------------------------------------------------------------------------
# sublane dispatch-worker --execute (ack drive).
# ---------------------------------------------------------------------------


class FakeWorkerDispatchOps:
    def __init__(self):
        self.calls = []

    def read_lane(self, worktree_path):
        return _lane()

    def dispatch_to_worker(self, **kwargs):
        self.calls.append("dispatch")
        return 0


def _worker_req(**kw):
    base = dict(
        issue=ISSUE,
        lane_label=LABEL,
        worktree_path="/wt/13290",
        journal="72669",
    )
    base.update(kw)
    return WorkerDispatchRequest(**base)


class WorkerDispatchGateTests(unittest.TestCase):
    def test_stop_without_override_fails_closed_before_send(self):
        ops = FakeWorkerDispatchOps()
        outcome = WorkerDispatchUseCase(ops).run(
            _worker_req(), execute=True, fill_inputs=_stop_inputs()
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_FILL_STOP, outcome.blocked_reasons)
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        # Fail closed before the lane is even probed / the worker send attempted.
        self.assertEqual(ops.calls, [])

    def test_stop_with_override_proceeds_and_records(self):
        ops = FakeWorkerDispatchOps()
        outcome = WorkerDispatchUseCase(ops).run(
            _worker_req(),
            execute=True,
            fill_inputs=_stop_inputs(),
            override_fill_stop="owner intent #13229 j#72635",
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_WORKER_DISPATCHED)
        self.assertEqual(
            outcome.fill_override_reason, "owner intent #13229 j#72635"
        )
        self.assertIn("dispatch", ops.calls)
        journal = render_worker_dispatch_journal(outcome)
        self.assertIn("- fill_stop_override: owner intent #13229 j#72635", journal)

    def test_dispatch_next_proceeds(self):
        ops = FakeWorkerDispatchOps()
        outcome = WorkerDispatchUseCase(ops).run(
            _worker_req(), execute=True, fill_inputs=_dispatch_next_inputs()
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIn("dispatch", ops.calls)

    def test_no_fill_context_is_back_compat(self):
        ops = FakeWorkerDispatchOps()
        outcome = WorkerDispatchUseCase(ops).run(_worker_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIsNone(outcome.fill_decision)

    def test_override_survives_a_later_execution_failure(self):
        # Override passes the gate, then no live lane resolves: the block record
        # must still carry the override so the durable journal stays honest.
        class NoLaneOps(FakeWorkerDispatchOps):
            def read_lane(self, worktree_path):
                return None

        ops = NoLaneOps()
        outcome = WorkerDispatchUseCase(ops).run(
            _worker_req(),
            execute=True,
            fill_inputs=_stop_inputs(),
            override_fill_stop="owner intent #13229 j#72635",
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertNotIn(REASON_FILL_STOP, outcome.blocked_reasons)
        self.assertEqual(ops.calls, [])
        self.assertEqual(outcome.fill_decision, FILL_STOP_COORDINATOR_BLOCKING)
        self.assertEqual(
            outcome.fill_override_reason, "owner intent #13229 j#72635"
        )
        journal = render_worker_dispatch_journal(outcome)
        self.assertIn(
            "- fill_stop_override: owner intent #13229 j#72635", journal
        )


# ---------------------------------------------------------------------------
# CLI arg binding (arming logic).
# ---------------------------------------------------------------------------


class ResolveDispatchAdmissionArgsTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(
            lane=None,
            ready_independent=0,
            ready_overlap=0,
            capacity=0,
            owner_or_release_gate=False,
            override_fill_stop=None,
        )
        base.update(kw)
        return Namespace(**base)

    def test_no_flags_is_not_armed(self):
        fill_inputs, override = resolve_dispatch_admission_args(self._args())
        self.assertIsNone(fill_inputs)
        self.assertIsNone(override)

    def test_lane_flag_arms(self):
        fill_inputs, _ = resolve_dispatch_admission_args(
            self._args(lane=[LaneState(issue="1", state_class="implementing")])
        )
        self.assertIsNotNone(fill_inputs)
        self.assertEqual(len(fill_inputs.lanes), 1)

    def test_counts_arm(self):
        fill_inputs, _ = resolve_dispatch_admission_args(
            self._args(ready_independent=2, capacity=1)
        )
        self.assertIsNotNone(fill_inputs)
        self.assertEqual(fill_inputs.ready_independent_work, 2)
        self.assertEqual(fill_inputs.capacity_remaining, 1)

    def test_override_alone_arms_and_is_trimmed(self):
        fill_inputs, override = resolve_dispatch_admission_args(
            self._args(override_fill_stop="  reason  ")
        )
        self.assertIsNotNone(fill_inputs)
        self.assertEqual(override, "reason")

    def test_armed_stop_with_override_flows_through_to_gate(self):
        # End-to-end: the bound inputs feed the gate to an overridden decision.
        fill_inputs, override = resolve_dispatch_admission_args(
            self._args(
                lane=[LaneState(issue="2", state_class="owner_waiting")],
                ready_independent=1,
                capacity=1,
                override_fill_stop="explicit",
            )
        )
        decision = evaluate_dispatch_admission(fill_inputs, override_reason=override)
        self.assertEqual(decision.gate, FILL_GATE_STOP_OVERRIDDEN)


if __name__ == "__main__":
    unittest.main()
