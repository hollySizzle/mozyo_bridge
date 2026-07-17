"""Regression contract for #13933's separate pending-composer preparation rail."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.core.state.replacement_preservation import assess_preservation
from mozyo_bridge.core.state.replacement_transaction import ParticipantPin
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PreparationDrive,
    PreparationObservation,
    PrepareBoundPairRequest,
    run_bound_pair_preparation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard_live import (
    LiveBoundPairPreparationOps,
    _ComposerDiscardActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    BoundPairObservation,
    ConvergeBoundPairRequest,
    PinRepairResult,
    ReplacementDrive,
    run_bound_pair_convergence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (
    APPROVAL_GATE,
    BLOCK_APPROVAL_MISSING,
    BLOCK_NO_DISCARDABLE_COMPOSER,
    BLOCK_PAIR_PRESERVED,
    STATE_ACTIONABLE,
    STATE_PREPARED,
    PreparationExpectation,
    approval_matches,
    expectation_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    BLOCK_PAIR_PRESERVED as CONVERGENCE_PAIR_PRESERVED,
    BoundSlot,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_PRESERVE_PENDING,
    SLOT_PRESERVE_PRODUCTIVE,
    SLOT_RECOVER,
    SlotRecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    OLD_SLOT_AMBIGUOUS,
)


REQ = PrepareBoundPairRequest(
    issue="13933",
    journal="80908",
    lane="issue_13933_bound_stale_pair_convergence",
    worktree="/tmp/wt-13933",
    branch="issue_13933_bound_stale_pair_convergence",
)


def _slot(role: str, disposition: str) -> BoundSlot:
    provider = "codex" if role == "gateway" else "claude"
    locator = "w1:p1" if role == "gateway" else "w1:p2"
    return BoundSlot(role, provider, f"managed-{role}", locator, disposition)


def _observation(**changes) -> PreparationObservation:
    values = dict(
        workspace_id="mzb1_workspace",
        worktree_path=REQ.worktree,
        worktree_identity="wt_deadbeef",
        branch=REQ.branch,
        revision=4,
        generation=1,
        lifecycle_exact=True,
        pins_empty=True,
        inventory_readable=True,
        worktree_readable=True,
        worktree_clean=True,
        branch_matches=True,
        slots=(
            _slot("gateway", SLOT_PRESERVE_PENDING),
            _slot("worker", SLOT_RECOVER),
        ),
        discard_roles=("gateway",),
    )
    values.update(changes)
    return PreparationObservation(**values)


class FakeOps:
    def __init__(self, observation=None):
        self.observation = observation or _observation()
        self.markers = ()
        self.drive_result = PreparationDrive(True, "recovered")
        self.calls = []

    def observe(self, request, *, action_id=""):
        self.calls.append(("observe", action_id))
        return self.observation

    def approval_fields(self, issue, journal):
        self.calls.append(("approval", issue, journal))
        return self.markers

    def drive(self, request, expectation, initial):
        self.calls.append(("drive", expectation.action_id))
        return self.drive_result


def _authorize(ops: FakeOps):
    preflight = run_bound_pair_preparation(REQ, execute=False, ops=ops)
    [(channel, fields)] = marker_fields_in_note(preflight.approval_marker)
    assert channel == "workflow-event"
    ops.markers = (fields,)
    return preflight, fields


class PreparationAuthorityTests(unittest.TestCase):
    def test_preflight_emits_exact_distinct_owner_marker_without_effect(self):
        ops = FakeOps()
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertEqual(outcome.state, STATE_ACTIONABLE)
        self.assertIn(f"gate={APPROVAL_GATE}", outcome.approval_marker)
        self.assertIn("discard_roles=gateway", outcome.approval_marker)
        self.assertFalse(outcome.executed)
        self.assertEqual(ops.calls, [("observe", "")])

    def test_marker_binds_branch_worktree_slots_and_discard_role_set(self):
        observation = _observation()
        expectation = expectation_for(
            issue=REQ.issue,
            lane=REQ.lane,
            revision=observation.revision,
            generation=observation.generation,
            resolved_worktree=observation.worktree_path,
            worktree_identity=observation.worktree_identity,
            branch=observation.branch,
            slots=observation.slots,
            discard_roles=observation.discard_roles,
        )
        fields = expectation.marker_fields()
        self.assertTrue(expectation.self_consistent())
        self.assertTrue(approval_matches(fields, expectation))
        for changed in (
            expectation.__class__(**{**expectation.__dict__, "worktree_digest": "other"}),
            expectation.__class__(**{**expectation.__dict__, "slot_digest": "other"}),
            expectation.__class__(**{**expectation.__dict__, "discard_roles": ("worker",)}),
        ):
            self.assertFalse(changed.self_consistent())

    def test_execute_requires_fresh_structured_approval_not_prose(self):
        ops = FakeOps()
        outcome = run_bound_pair_preparation(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.reason, BLOCK_APPROVAL_MISSING)
        self.assertFalse(any(call[0] == "drive" for call in ops.calls))

    def test_exact_approval_reaches_only_the_preparation_drive(self):
        ops = FakeOps()
        _authorize(ops)
        outcome = run_bound_pair_preparation(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.state, STATE_PREPARED)
        self.assertTrue(outcome.executed)
        self.assertEqual([call[0] for call in ops.calls].count("drive"), 1)
        self.assertFalse(outcome.as_payload()["pins_repaired"])
        self.assertFalse(outcome.as_payload()["resumed"])
        self.assertFalse(outcome.as_payload()["sent"])

    def test_no_pending_or_non_discardable_preserved_slot_is_zero_effect(self):
        no_pending = _observation(
            slots=(_slot("gateway", SLOT_HEALTHY), _slot("worker", SLOT_RECOVER)),
            discard_roles=(),
        )
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=FakeOps(no_pending))
        self.assertEqual(outcome.reason, BLOCK_NO_DISCARDABLE_COMPOSER)

        productive = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PRODUCTIVE),
            )
        )
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=FakeOps(productive))
        self.assertEqual(outcome.reason, BLOCK_PAIR_PRESERVED)

    def test_existing_convergence_pending_hard_block_is_unchanged(self):
        request = ConvergeBoundPairRequest(**REQ.__dict__)
        observed = BoundPairObservation(
            workspace_id="mzb1_workspace",
            worktree_path=REQ.worktree,
            worktree_identity="wt_deadbeef",
            branch=REQ.branch,
            revision=4,
            generation=1,
            lifecycle_exact=True,
            pins_empty=True,
            inventory_readable=True,
            worktree_readable=True,
            worktree_clean=True,
            branch_matches=True,
            slots=_observation().slots,
        )

        class ExistingOps:
            def observe(self, request, *, action_id=""):
                return observed

            def approval_fields(self, issue, journal):
                raise AssertionError("approval must not be read")

            def drive_replacement(self, *args):
                raise AssertionError("must not drive")

            def final_pins(self, *args, **kwargs):
                return observed, ()

            def repair_pins(self, *args):
                return PinRepairResult(False, "must_not_run")

            def finish_replacement(self, *args):
                return False

        outcome = run_bound_pair_convergence(request, execute=True, ops=ExistingOps())
        self.assertEqual(outcome.verdict.reason, CONVERGENCE_PAIR_PRESERVED)
        self.assertFalse(outcome.executed)


class CloseBoundaryTests(unittest.TestCase):
    def _port(self, *, composer_ok=True, disposition=SLOT_PRESERVE_PENDING):
        owner = LiveBoundPairPreparationOps(repo_root=Path("/coordinator"), env={})
        owner._composer_discardable = mock.Mock(return_value=composer_ok)
        owner._lifecycle = mock.Mock(
            return_value=SimpleNamespace(
                lane_disposition="hibernated",
                binding_kind="issue",
                issue_id=REQ.issue,
                project_scope="",
                worktree_identity="wt_deadbeef",
                process_release="released",
                replacement_state="not_requested",
                revision=4,
                lane_generation=1,
            )
        )
        owner._worktree = mock.Mock(
            return_value=(Path(REQ.worktree), "mzb1_workspace", "wt_deadbeef")
        )
        slot_observation = SlotRecoveryObservation(
            identity_resolved=True,
            belongs_to_pair=True,
            generation_not_newer=True,
            not_productive=disposition != SLOT_PRESERVE_PRODUCTIVE,
            no_pending_composer=disposition != SLOT_PRESERVE_PENDING,
            worktree_readable=True,
            is_bad_generation=True,
        )
        live = SimpleNamespace(
            snapshot_rows=(),
            workspace_id=lambda: "mzb1_workspace",
            observe_slot=lambda **kwargs: (
                slot_observation, "w1:p1", "managed-gateway"
            ),
        )
        request = ConvergeBoundPairRequest(**REQ.__dict__)
        expectation = SimpleNamespace(action_id="action")
        return _ComposerDiscardActuatorPort(
            owner, request, expectation, live, REQ, ("gateway",)
        )

    @staticmethod
    def _pin():
        return ParticipantPin(
            lane_id=REQ.lane,
            role="gateway",
            provider="codex",
            assigned_name="managed-gateway",
            old_locator="w1:p1",
            is_self=False,
            lane_revision="4",
            lane_generation="1",
        )

    def _observe(self, port, *, branch=REQ.branch, status=""):
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_composer_discard_live"
        )

        def git_result(_worktree, *args):
            return (True, branch) if args == ("branch", "--show-current") else (True, status)

        with mock.patch(f"{module}.list_herdr_agent_rows", return_value=()), mock.patch(
            f"{module}._git", side_effect=git_result
        ):
            return assess_preservation(port.observe_preservation(self._pin()))

    def test_exact_approved_uncorrelated_pending_slot_is_the_only_close_carveout(self):
        self.assertTrue(self._observe(self._port()).may_close)
        self.assertTrue(self._observe(self._port(composer_ok=False)).blocked)
        self.assertTrue(
            self._observe(self._port(disposition=SLOT_PRESERVE_PRODUCTIVE)).blocked
        )
        self.assertTrue(self._observe(self._port(), branch="other").blocked)
        self.assertTrue(self._observe(self._port(), status=" M guarded.txt").blocked)

    def test_absent_close_owed_slot_without_transaction_close_proof_is_ambiguous(self):
        port = self._port()
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.list_herdr_agent_rows"
        )
        with mock.patch(module, return_value=()):
            self.assertEqual(port.observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
