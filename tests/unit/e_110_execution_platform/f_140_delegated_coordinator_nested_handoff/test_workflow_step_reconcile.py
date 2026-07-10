"""Unit tests for the `workflow step` <-> runtime-store reconcile bridge (Redmine #13291).

Pins the fixed-vocabulary composition rules of
:mod:`...domain.workflow_step_reconcile`:

- absent / unavailable store -> the live outcome is byte-identical (backward compatible);
- a present but non-pending store action (none / hold / await) -> live unchanged;
- a pending non-gating store action -> surfaced (aligned), live unchanged;
- a pending *gating* store action (requires_confirmation / blocked) + a live forward
  (``ready``) leg -> the forward leg is fail-closed gated (store wins, fail-toward-safe);
- a gating store action against an already-blocked / no-op live leg -> surfaced only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    BLOCKED_ROUTE_IDENTITY_UNRESOLVED,
    RISK_HIGH,
    RISK_LOW,
    RISK_NONE,
    WorkflowNextAction,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    EXECUTION_READY,
    OWNER_OPERATOR,
    OWNER_PARENT,
    PRIMITIVE_CONSULT,
    PRIMITIVE_NONE,
    REASON_ANCHOR_REQUIRED,
    REASON_CONSULTATION_READY,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDPARENT_CONSULTATION,
    WorkflowStepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_reconcile import (
    REASON_STORE_PENDING_ACTION_GATES,
    RECONCILE_STORE_ABSENT,
    RECONCILE_STORE_ALIGNED,
    RECONCILE_STORE_GATES_LIVE,
    RECONCILE_STORE_ISSUE_MISMATCH,
    RECONCILE_STORE_NO_PENDING,
    RECONCILE_STORE_UNAVAILABLE,
    STORE_ABSENT,
    STORE_PRESENT,
    STORE_UNAVAILABLE,
    reconcile_step_with_store,
    store_action_is_gating,
    store_action_is_pending,
)


def _live_ready() -> WorkflowStepOutcome:
    """A live executable forward leg (grandparent consultation)."""
    return WorkflowStepOutcome(
        state=STATE_GRANDPARENT_CONSULTATION,
        next_action="forward the ticketless consultation",
        execution=EXECUTION_READY,
        reason=REASON_CONSULTATION_READY,
        next_owner=OWNER_PARENT,
        primitive=PRIMITIVE_CONSULT,
        target_pane="%gw",
        repo_root="/work/repo",
        project_scope="cloud-drive",
        self_pane="%self",
    )


def _live_blocked() -> WorkflowStepOutcome:
    """A live fail-closed leg (child anchor required)."""
    return WorkflowStepOutcome(
        state=STATE_CHILD_WORKER_DISPATCH,
        next_action="decide the Redmine anchor",
        execution=EXECUTION_BLOCKED,
        reason=REASON_ANCHOR_REQUIRED,
        next_owner="child",
        primitive=PRIMITIVE_NONE,
        self_pane="%self",
    )


def _na(action, *, owner_role="coordinator", risk=RISK_LOW, confirm=False, blocked=""):
    return WorkflowNextAction(
        action=action,
        owner_role=owner_role,
        target_issue="13291",
        route_identity="route=r ws=w lane=l role=codex pane_name=gw",
        anchor="13291:72672",
        suggested_command="mozyo-bridge workflow resume",
        risk_level=risk,
        requires_confirmation=confirm,
        blocked_reason=blocked,
        reason="test",
        provider="codex",
    )


class IssueCorrelationTest(unittest.TestCase):
    """Issue-correlation of the store action with a herdr live-verified anchor (Redmine #13489 F3c)."""

    def test_matching_issue_reconciles_normally(self):
        rec = reconcile_step_with_store(
            _live_blocked(), _na("perform_review"),
            store_status=STORE_PRESENT, live_anchor_issue="13291",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ALIGNED)

    def test_mismatched_issue_is_rejected_not_aligned(self):
        rec = reconcile_step_with_store(
            _live_blocked(), _na("perform_review"),
            store_status=STORE_PRESENT, live_anchor_issue="99999",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ISSUE_MISMATCH)
        self.assertIs(rec.outcome, rec.live_outcome)  # live outcome unchanged

    def test_mismatched_gating_action_does_not_gate_a_ready_leg(self):
        rec = reconcile_step_with_store(
            _live_ready(), _na("integrate", confirm=True),
            store_status=STORE_PRESENT, live_anchor_issue="99999",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ISSUE_MISMATCH)
        self.assertEqual(rec.outcome.execution, "ready")  # NOT downgraded to blocked

    def test_none_anchor_issue_is_byte_invariant_tmux(self):
        # The tmux path passes None -> no correlation constraint -> prior behaviour (gates).
        rec = reconcile_step_with_store(
            _live_ready(), _na("integrate", confirm=True),
            store_status=STORE_PRESENT, live_anchor_issue=None,
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_GATES_LIVE)

    def test_anchor_field_issue_match(self):
        import dataclasses

        na = dataclasses.replace(_na("perform_review"), target_issue="")  # anchor "13291:72672"
        rec = reconcile_step_with_store(
            _live_blocked(), na, store_status=STORE_PRESENT, live_anchor_issue="13291",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ALIGNED)

    def test_internally_contradictory_target_live_anchor_other_is_rejected(self):
        # F3c-2: target_issue matches live but the anchor names a different issue -> not a match.
        import dataclasses

        na = dataclasses.replace(_na("perform_review"), anchor="99999:1")  # target_issue=13291
        rec = reconcile_step_with_store(
            _live_blocked(), na, store_status=STORE_PRESENT, live_anchor_issue="13291",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ISSUE_MISMATCH)

    def test_internally_contradictory_target_other_anchor_live_is_rejected(self):
        import dataclasses

        na = dataclasses.replace(_na("perform_review"), target_issue="99999")  # anchor=13291:72672
        rec = reconcile_step_with_store(
            _live_blocked(), na, store_status=STORE_PRESENT, live_anchor_issue="13291",
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ISSUE_MISMATCH)


class PredicateTest(unittest.TestCase):
    def test_none_hold_await_are_not_pending(self):
        for action in ("none", "hold", "await_implementation"):
            self.assertFalse(store_action_is_pending(_na(action)))

    def test_other_actions_are_pending(self):
        self.assertTrue(store_action_is_pending(_na("perform_review")))
        self.assertTrue(store_action_is_pending(_na("integrate", confirm=True)))

    def test_gating_uses_store_own_vocabulary(self):
        self.assertTrue(store_action_is_gating(_na("integrate", confirm=True)))
        self.assertTrue(
            store_action_is_gating(
                _na("perform_review", blocked=BLOCKED_ROUTE_IDENTITY_UNRESOLVED)
            )
        )
        self.assertFalse(store_action_is_gating(_na("deliver_callback")))


class DegradeTest(unittest.TestCase):
    def test_absent_store_leaves_live_identical(self):
        live = _live_ready()
        rec = reconcile_step_with_store(live, None, store_status=STORE_ABSENT)
        self.assertEqual(rec.disposition, RECONCILE_STORE_ABSENT)
        self.assertIs(rec.outcome, live)
        self.assertFalse(rec.reflects_store)
        self.assertEqual(rec.reconcile_payload_fields(), {})
        self.assertEqual(rec.reconcile_text_lines(), [])
        self.assertTrue(rec.outcome.executable)

    def test_unavailable_store_degrades_to_live(self):
        live = _live_ready()
        rec = reconcile_step_with_store(live, None, store_status=STORE_UNAVAILABLE)
        self.assertEqual(rec.disposition, RECONCILE_STORE_UNAVAILABLE)
        self.assertIs(rec.outcome, live)
        self.assertFalse(rec.reflects_store)

    def test_present_but_missing_action_degrades(self):
        # store_status present but no action object -> treated as unavailable, never crashes.
        live = _live_ready()
        rec = reconcile_step_with_store(live, None, store_status=STORE_PRESENT)
        self.assertEqual(rec.disposition, RECONCILE_STORE_UNAVAILABLE)
        self.assertIs(rec.outcome, live)


class NoPendingTest(unittest.TestCase):
    def test_hold_action_is_no_pending(self):
        live = _live_ready()
        rec = reconcile_step_with_store(
            live, _na("hold", risk=RISK_NONE), store_status=STORE_PRESENT
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_NO_PENDING)
        self.assertIs(rec.outcome, live)
        self.assertFalse(rec.reflects_store)
        self.assertTrue(rec.outcome.executable)


class AlignedTest(unittest.TestCase):
    def test_pending_non_gating_is_surfaced_without_changing_route(self):
        live = _live_ready()
        rec = reconcile_step_with_store(
            live, _na("deliver_callback"), store_status=STORE_PRESENT
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ALIGNED)
        # The live forward leg is unchanged (still executable) ...
        self.assertIs(rec.outcome, live)
        self.assertTrue(rec.outcome.executable)
        # ... but the store action is now reflected in the reported output.
        self.assertTrue(rec.reflects_store)
        fields = rec.reconcile_payload_fields()
        self.assertEqual(fields["reconcile_disposition"], RECONCILE_STORE_ALIGNED)
        self.assertEqual(fields["store_pending_action"]["action"], "deliver_callback")
        self.assertTrue(rec.reconcile_text_lines())

    def test_gating_action_against_blocked_live_is_only_surfaced(self):
        # A gating store action does not transform an already-not-forward live leg.
        live = _live_blocked()
        rec = reconcile_step_with_store(
            live, _na("integrate", risk=RISK_HIGH, confirm=True), store_status=STORE_PRESENT
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ALIGNED)
        self.assertIs(rec.outcome, live)
        self.assertFalse(rec.outcome.executable)  # still the original blocked leg
        self.assertEqual(rec.outcome.reason, REASON_ANCHOR_REQUIRED)
        self.assertTrue(rec.reflects_store)

    def test_gating_action_against_no_op_live_is_only_surfaced(self):
        live = WorkflowStepOutcome(
            state="grandchild_redmine_work",
            next_action="read the anchor and implement",
            execution=EXECUTION_NO_OP,
            reason="redmine_work_ready",
            next_owner="grandchild",
            primitive=PRIMITIVE_NONE,
            self_pane="%self",
        )
        rec = reconcile_step_with_store(
            live, _na("close_issue", risk=RISK_HIGH, confirm=True), store_status=STORE_PRESENT
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_ALIGNED)
        self.assertIs(rec.outcome, live)


class GatesLiveTest(unittest.TestCase):
    def test_gating_action_downgrades_forward_leg_to_blocked(self):
        live = _live_ready()
        rec = reconcile_step_with_store(
            live, _na("integrate", risk=RISK_HIGH, confirm=True), store_status=STORE_PRESENT
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_GATES_LIVE)
        gated = rec.outcome
        # Fail-toward-safe: the forward leg is no longer executable.
        self.assertFalse(gated.executable)
        self.assertEqual(gated.execution, EXECUTION_BLOCKED)
        self.assertEqual(gated.reason, REASON_STORE_PENDING_ACTION_GATES)
        self.assertEqual(gated.next_owner, OWNER_OPERATOR)
        self.assertFalse(gated.ok)
        # The resolved live routing context is preserved for the operator.
        self.assertEqual(gated.state, STATE_GRANDPARENT_CONSULTATION)
        self.assertEqual(gated.target_pane, "%gw")
        self.assertIn("integrate", gated.next_action)
        self.assertIn("workflow resume", gated.next_action)
        # The live outcome is retained untouched alongside the gated one.
        self.assertIs(rec.live_outcome, live)
        self.assertEqual(rec.live_outcome.execution, EXECUTION_READY)
        # The store action is reflected.
        fields = rec.reconcile_payload_fields()
        self.assertEqual(fields["store_pending_action"]["action"], "integrate")
        self.assertTrue(fields["store_pending_action"]["requires_confirmation"])

    def test_blocked_reason_store_action_gates_forward_leg(self):
        live = _live_ready()
        rec = reconcile_step_with_store(
            live,
            _na("perform_review", blocked=BLOCKED_ROUTE_IDENTITY_UNRESOLVED, confirm=True),
            store_status=STORE_PRESENT,
        )
        self.assertEqual(rec.disposition, RECONCILE_STORE_GATES_LIVE)
        self.assertFalse(rec.outcome.executable)
        self.assertIn("blocked_reason=route_identity_unresolved", rec.outcome.next_action)


if __name__ == "__main__":
    unittest.main()
