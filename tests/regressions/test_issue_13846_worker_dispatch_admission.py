"""Regression coverage for action-time worker dispatch admission (#13846)."""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (
    WorkerDispatchUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    ACTUATE_BLOCKED,
    DISPATCH_WORKER_DISPATCHED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (
    ADMISSION_HEALTHY,
    ADMISSION_STALE_WORKER_RECOVERY_REQUIRED,
    ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
    WORKER_DISPATCH_TURN_START_UNCONFIRMED,
    WorkerDispatchAdmission,
    WorkerDispatchAdmissionFacts,
    WorkerDispatchRequest,
    decide_worker_dispatch_admission,
    render_worker_dispatch_journal,
)


def _facts(**overrides) -> WorkerDispatchAdmissionFacts:
    values = dict(
        lifecycle_current=True,
        anchor_current=True,
        identity_attested=True,
        action_binding_current=True,
        slot_state="live",
        locator_present=True,
        receiver_state="awaiting_input",
        workspace_id="ws",
        lane_id="issue_13846_lane",
        lane_generation=7,
        worker_assigned_name="mzb1_ws_claude_issueZ5F13846Z5Flane",
        worker_locator="w28:p75",
    )
    values.update(overrides)
    return WorkerDispatchAdmissionFacts(**values)


def _decision(**overrides) -> WorkerDispatchAdmission:
    return decide_worker_dispatch_admission(_facts(**overrides))


def _lane() -> SublaneLaneView:
    return SublaneLaneView(
        workspace_id="ws",
        lane_id="issue_13846_lane",
        lane_label="issue_13846_lane",
        issue="13846",
        branch="issue_13846",
        repo_root="/lane/13846",
        gateway_pane="w28:p74",
        worker_pane="w28:p75",
        state="active",
    )


def _request() -> WorkerDispatchRequest:
    return WorkerDispatchRequest(
        issue="13846",
        lane_label="issue_13846_lane",
        worktree_path="/lane/13846",
        journal="81683",
    )


class _Ops:
    def __init__(self, admissions, *, turn_start="started"):
        self.admissions = list(admissions)
        self.turn_start = turn_start
        self.sent = 0

    def read_lane(self, worktree_path):
        return _lane()

    def observe_worker_dispatch_admission(self, **kwargs):
        if len(self.admissions) > 1:
            return self.admissions.pop(0)
        return self.admissions[0]

    def probe_worker_ready(self, worker_pane):
        return True

    def dispatch_to_worker(self, **kwargs):
        self.sent += 1
        return 0

    def dispatch_to_worker_turn_start(self, **kwargs):
        kwargs.pop("worker_assigned_name", None)
        return self.dispatch_to_worker(**kwargs), self.turn_start

    def reserve_worker_dispatch(self, **kwargs):
        return True, "reserved"

    def complete_worker_dispatch(self, **kwargs):
        return True


class AdmissionDecisionTests(unittest.TestCase):
    def test_locator_bearing_stale_token_is_conflict_not_recovery_permission(self):
        result = _decision(slot_state="stale_named_slot")
        self.assertEqual(
            result.decision, ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT
        )
        self.assertFalse(result.retry_allowed)

    def test_only_authoritative_terminal_absence_routes_stale_recovery(self):
        result = _decision(
            slot_state="stale_named_slot",
            locator_present=False,
            worker_locator=None,
            receiver_state="absent",
            identity_attested=False,
            terminal_absence_authoritative=True,
        )
        self.assertEqual(
            result.decision, ADMISSION_STALE_WORKER_RECOVERY_REQUIRED
        )

    def test_missing_generation_attestation_or_duplicate_delivery_conflicts(self):
        for changes in (
            {"lifecycle_current": False},
            {"identity_attested": False},
            {"duplicate_or_uncertain_delivery": True},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    _decision(**changes).decision,
                    ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
                )


class ActionBoundaryTests(unittest.TestCase):
    def test_post_probe_authority_change_is_zero_send(self):
        ops = _Ops(
            [
                _decision(),
                _decision(action_binding_current=False),
            ]
        )
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=1).run(
            _request(), execute=True
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertEqual(ops.sent, 0)
        self.assertEqual(
            outcome.blocked_reasons,
            (ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,),
        )

    def test_transport_ack_without_turn_start_never_promotes_or_retries(self):
        ops = _Ops([_decision()], turn_start="unknown")
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=1).run(
            _request(), execute=True
        )
        self.assertEqual(ops.sent, 1)
        self.assertEqual(
            outcome.dispatch_result, WORKER_DISPATCH_TURN_START_UNCONFIRMED
        )
        self.assertFalse(outcome.worker_dispatch_confirmed)
        self.assertFalse(outcome.retry_allowed)

    def test_only_healthy_ack_and_started_promotes_and_preserves_callback_wait(self):
        ops = _Ops([_decision()])
        outcome = WorkerDispatchUseCase(ops, worker_ready_probes=1).run(
            _request(), execute=True
        )
        self.assertEqual(outcome.admission_decision, ADMISSION_HEALTHY)
        self.assertEqual(outcome.dispatch_result, DISPATCH_WORKER_DISPATCHED)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        self.assertIn("coordinator-callback", render_worker_dispatch_journal(outcome))


if __name__ == "__main__":
    unittest.main()
