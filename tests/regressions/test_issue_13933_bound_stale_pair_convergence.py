"""Regression matrix for #13933's bounded hibernated pair convergence rail."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.core.state.lane_lifecycle import ProcessGenerationPin
from mozyo_bridge.core.state.replacement_preservation import assess_preservation
from mozyo_bridge.core.state.replacement_transaction import ParticipantPin
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    BoundPairObservation,
    ConvergeBoundPairRequest,
    PinRepairResult,
    ReplacementDrive,
    run_bound_pair_convergence,
    transaction_plan_observation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (
    _BoundPairActuatorPort,
    _SnapshotRecoveryOps,
    LiveBoundPairConvergenceOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    APPROVAL_GATE,
    BLOCK_APPROVAL_MISMATCH,
    BLOCK_APPROVAL_MISSING,
    BLOCK_FRESH_PAIR_UNPROVEN,
    BLOCK_INVENTORY_UNREADABLE,
    BLOCK_NOT_BOUND_SIGNATURE,
    BLOCK_PAIR_AMBIGUOUS,
    BLOCK_PAIR_PRESERVED,
    BLOCK_PIN_CAS_REFUSED,
    BLOCK_REPLACEMENT_STOPPED,
    BLOCK_WORKTREE_UNSAFE,
    STATE_ACTIONABLE,
    STATE_ALREADY_CONVERGED,
    BoundSlot,
    approval_matches,
    decide_transaction_plan,
    slot_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (
    SLOT_HEALTHY,
    SLOT_PRESERVE_AMBIGUOUS,
    SLOT_RECOVER,
    SlotRecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    LAUNCH_DONE,
    OLD_SLOT_AMBIGUOUS,
)


REQ = ConvergeBoundPairRequest(
    issue="13933",
    journal="80899",
    lane="issue_13933_bound_stale_pair_convergence",
    worktree="/tmp/wt-13933",
    branch="issue_13933_bound_stale_pair_convergence",
)


def _slot(role: str, *, disposition: str = SLOT_RECOVER, locator: str | None = None, proof: bool = False):
    provider = "codex" if role == "gateway" else "claude"
    return BoundSlot(
        role=role,
        provider=provider,
        assigned_name=f"managed-{role}",
        locator=locator if locator is not None else ("w1:p1" if role == "gateway" else "w1:p2"),
        disposition=disposition,
        close_proven=proof,
    )


def _observation(**changes):
    values = dict(
        workspace_id="mzb1_workspace",
        worktree_path=REQ.worktree,
        worktree_identity="wt_deadbeef",
        branch=REQ.branch,
        revision=4,
        generation=1,
        lifecycle_exact=True,
        pins_empty=True,
        pins_exact=False,
        inventory_readable=True,
        worktree_readable=True,
        worktree_clean=True,
        branch_matches=True,
        slots=(_slot("gateway"), _slot("worker")),
    )
    values.update(changes)
    return BoundPairObservation(**values)


def _pins(observation):
    return tuple(
        ProcessGenerationPin(
            role=slot.role,
            provider=slot.provider,
            assigned_name=slot.assigned_name,
            locator=slot.locator,
            attested_at="2026-07-17T00:00:00Z",
        )
        for slot in observation.slots
    )


class FakeOps:
    def __init__(self, first=None):
        self.first = first or _observation()
        self.current = self.first
        self.action_current = None
        self.final = _observation(
            slots=(_slot("gateway", disposition=SLOT_HEALTHY, locator="w2:p1"),
                   _slot("worker", disposition=SLOT_HEALTHY, locator="w2:p2")),
        )
        self.markers = []
        self.drive = ReplacementDrive(True, "recovered")
        self.repair = PinRepairResult(True, "applied", repaired=True)
        self.finish = True
        self.calls = []

    def observe(self, request, *, action_id=""):
        self.calls.append(("observe", action_id))
        return self.action_current if action_id and self.action_current is not None else self.current

    def approval_fields(self, issue, journal):
        self.calls.append(("approval", issue, journal))
        return tuple(self.markers)

    def drive_replacement(self, request, expectation, observation):
        self.calls.append(("drive", expectation.action_id, tuple(observation.slots)))
        return self.drive

    def final_pins(self, request, *, action_id):
        self.calls.append(("final", action_id))
        try:
            pins = _pins(self.final)
        except Exception:
            pins = ()
        return self.final, pins

    def repair_pins(self, request, expectation, observation, pins):
        self.calls.append(("repair", expectation.action_id, tuple(pins)))
        return self.repair

    def finish_replacement(self, expectation):
        self.calls.append(("finish", expectation.action_id))
        return self.finish


def _authorize(ops: FakeOps):
    preflight = run_bound_pair_convergence(REQ, execute=False, ops=ops)
    markers = marker_fields_in_note(preflight.verdict.approval_marker)
    assert len(markers) == 1
    channel, fields = markers[0]
    assert channel == "workflow-event"
    ops.markers = [fields]
    return preflight, fields


class DomainContractTests(unittest.TestCase):
    def test_slot_digest_is_order_stable_but_generation_sensitive(self):
        a = (_slot("gateway"), _slot("worker"))
        self.assertEqual(slot_digest(a), slot_digest(tuple(reversed(a))))
        changed = (_slot("gateway", locator="w9:p9"), _slot("worker"))
        self.assertNotEqual(slot_digest(a), slot_digest(changed))

    def test_close_proof_does_not_fabricate_a_pin_or_change_approval_identity(self):
        original = _slot("gateway")
        replay = _slot("gateway", proof=True)
        self.assertEqual(slot_digest((original, _slot("worker"))), slot_digest((replay, _slot("worker"))))

    def test_marker_is_structured_exact_and_prose_is_not_authority(self):
        ops = FakeOps()
        preflight, fields = _authorize(ops)
        self.assertEqual(fields["gate"], APPROVAL_GATE)
        self.assertTrue(approval_matches(fields, preflight.verdict.approval_marker and _expectation_from(ops, fields)))

    def test_only_an_existing_transaction_may_resume_a_stable_progressed_pair(self):
        ops = FakeOps()
        _preflight, fields = _authorize(ops)
        expectation = _expectation_from(ops, fields)
        progressed = _observation(
            slots=(
                _slot("gateway", disposition=SLOT_HEALTHY, locator="w2:p1"),
                _slot("worker", disposition=SLOT_HEALTHY, locator="w2:p2"),
            )
        )
        observed = transaction_plan_observation(REQ, progressed)
        retry = decide_transaction_plan(
            expectation, observed, observed, transaction_exists=True
        )
        first_write = decide_transaction_plan(
            expectation, observed, observed, transaction_exists=False
        )
        self.assertTrue(retry.allowed)
        self.assertFalse(first_write.allowed)


def _expectation_from(ops, fields):
    # Test-only reconstruction exercises the complete field match without duplicating the
    # production action-id/digest derivation.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import ApprovalExpectation
    return ApprovalExpectation(
        issue=fields["issue"], lane=fields["lane"], revision=int(fields["revision"]),
        generation=int(fields["generation"]), action_generation=int(fields["action_generation"]),
        action_id=fields["action_id"], worktree_digest=fields["worktree_digest"],
        slot_digest=fields["slot_digest"],
    )


class OrchestrationTests(unittest.TestCase):
    def test_preflight_is_actionable_and_has_no_effect(self):
        ops = FakeOps()
        outcome = run_bound_pair_convergence(REQ, execute=False, ops=ops)
        self.assertEqual(outcome.verdict.state, STATE_ACTIONABLE)
        self.assertIn(f"gate={APPROVAL_GATE}", outcome.verdict.approval_marker)
        self.assertEqual(ops.calls, [("observe", "")])


class LiveActuatorBoundaryTests(unittest.TestCase):
    def _port(self):
        owner = SimpleNamespace(repo_root=Path("/coordinator"), env={})
        live = _SnapshotRecoveryOps(
            repo_root=owner.repo_root,
            request_issue=REQ.issue,
            request_lane=REQ.lane,
            request_journal=REQ.journal,
            env={},
        )
        live.target_workspace_id = "mzb1_workspace"
        return _BoundPairActuatorPort(owner, REQ, object(), live)

    def _pin(self):
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

    def test_unreadable_action_time_inventory_is_never_degraded_to_absent(self):
        port = self._port()
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.list_herdr_agent_rows"
        )
        with mock.patch(module, side_effect=RuntimeError("unreadable")):
            self.assertEqual(port.observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS)

    def test_action_bound_launch_targets_the_requested_worktree(self):
        port = self._port()
        calls = []

        class FakeActuator:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def heal_lane_column(self, worktree):
                calls.append(("heal", worktree))

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.HerdrSublaneActuatorOps"
        )
        with mock.patch(module, FakeActuator):
            result = port.launch_action_bound("action-13933", self._pin())
        self.assertEqual(result, LAUNCH_DONE)
        self.assertEqual(calls[0][1]["replacement_action_id"], "action-13933")
        self.assertEqual(calls[1], ("heal", REQ.worktree))

    def _close_boundary(self, record, *, branch=REQ.branch, status=""):
        owner = LiveBoundPairConvergenceOps(repo_root=Path("/coordinator"), env={})
        owner._lifecycle = mock.Mock(return_value=record)
        owner._worktree = mock.Mock(
            return_value=(Path(REQ.worktree), "mzb1_workspace", "wt_deadbeef")
        )
        slot_observation = SlotRecoveryObservation(
            identity_resolved=True,
            belongs_to_pair=True,
            generation_not_newer=True,
            not_productive=True,
            no_pending_composer=True,
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
        port = _BoundPairActuatorPort(owner, REQ, object(), live)

        def git_result(_worktree, *args):
            return (True, branch) if args == ("branch", "--show-current") else (True, status)

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live"
        )
        with mock.patch(f"{module}.list_herdr_agent_rows", return_value=()), mock.patch(
            f"{module}._git", side_effect=git_result
        ):
            return port.observe_preservation(self._pin())

    def _lifecycle_record(self, **changes):
        values = dict(
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
        values.update(changes)
        return SimpleNamespace(**values)

    def test_close_boundary_rechecks_exact_lifecycle_generation_and_clean_branch(self):
        baseline = assess_preservation(self._close_boundary(self._lifecycle_record()))
        self.assertTrue(baseline.may_close)

        races = (
            (self._lifecycle_record(revision=5), REQ.branch, ""),
            (self._lifecycle_record(lane_generation=2), REQ.branch, ""),
            (self._lifecycle_record(lane_disposition="active"), REQ.branch, ""),
            (self._lifecycle_record(process_release="requested"), REQ.branch, ""),
            (self._lifecycle_record(replacement_state="pending"), REQ.branch, ""),
            (self._lifecycle_record(), "other-branch", ""),
            (self._lifecycle_record(), REQ.branch, " M guarded.txt"),
        )
        for record, branch, status in races:
            with self.subTest(record=record, branch=branch, status=status):
                verdict = assess_preservation(
                    self._close_boundary(record, branch=branch, status=status)
                )
                self.assertTrue(verdict.blocked)

    def test_transaction_plan_races_are_zero_write(self):
        initial = _observation()
        auth_ops = FakeOps(initial)
        _preflight, fields = _authorize(auth_ops)
        expectation = _expectation_from(auth_ops, fields)

        races = (
            _observation(revision=5),
            _observation(generation=2),
            _observation(lifecycle_exact=False),
            _observation(inventory_readable=False),
            _observation(worktree_clean=False),
            _observation(branch="other-branch", branch_matches=False),
            _observation(slots=(_slot("gateway", locator="w9:p9"), _slot("worker"))),
        )

        class RecordingStore:
            def __init__(self):
                self.plan_calls = 0

            def get(self, key):
                return None

            def plan_transaction(self, *args, **kwargs):
                self.plan_calls += 1
                return SimpleNamespace(applied=False, reason="test_refusal")

        for fresh in races:
            with self.subTest(fresh=fresh):
                store = RecordingStore()
                ops = LiveBoundPairConvergenceOps(
                    repo_root=Path("/coordinator"), env={}, transaction_store=store
                )
                ops.observe = mock.Mock(return_value=fresh)
                result = ops.drive_replacement(REQ, expectation, initial)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "transaction_conflict")
                self.assertEqual(store.plan_calls, 0)

    def test_transaction_plan_write_requires_exact_stable_approved_snapshot(self):
        initial = _observation()
        auth_ops = FakeOps(initial)
        _preflight, fields = _authorize(auth_ops)
        expectation = _expectation_from(auth_ops, fields)

        class RecordingStore:
            plan_calls = 0

            def get(self, key):
                return None

            def plan_transaction(self, *args, **kwargs):
                self.plan_calls += 1
                return SimpleNamespace(applied=False, reason="test_refusal")

        store = RecordingStore()
        ops = LiveBoundPairConvergenceOps(
            repo_root=Path("/coordinator"), env={}, transaction_store=store
        )
        ops.observe = mock.Mock(return_value=initial)
        result = ops.drive_replacement(REQ, expectation, initial)
        self.assertFalse(result.ok)
        self.assertEqual(store.plan_calls, 1)

    def test_unreadable_inventory_is_zero_effect(self):
        ops = FakeOps(_observation(inventory_readable=False))
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_INVENTORY_UNREADABLE)
        self.assertFalse(any(call[0] == "drive" for call in ops.calls))

    def test_wrong_lifecycle_signature_is_zero_effect(self):
        ops = FakeOps(_observation(lifecycle_exact=False))
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_NOT_BOUND_SIGNATURE)

    def test_dirty_unreadable_or_branch_mismatch_are_zero_close(self):
        for change in (
            {"worktree_clean": False},
            {"worktree_readable": False},
            {"branch_matches": False},
        ):
            with self.subTest(change=change):
                ops = FakeOps(_observation(**change))
                outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
                self.assertEqual(outcome.verdict.reason, BLOCK_WORKTREE_UNSAFE)
                self.assertFalse(any(call[0] == "drive" for call in ops.calls))

    def test_duplicate_half_or_absent_without_transaction_proof_blocks(self):
        shapes = (
            (_slot("gateway"),),
            (_slot("gateway"), _slot("gateway", locator="w1:p9")),
            (_slot("gateway", locator=""), _slot("worker")),
        )
        for slots in shapes:
            with self.subTest(slots=slots):
                ops = FakeOps(_observation(slots=slots))
                outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
                self.assertEqual(outcome.verdict.reason, BLOCK_PAIR_AMBIGUOUS)

    def test_absent_with_same_transaction_close_proof_is_replayable(self):
        ops = FakeOps(_observation(slots=(_slot("gateway", locator="", proof=True), _slot("worker"))))
        outcome = run_bound_pair_convergence(REQ, execute=False, ops=ops)
        self.assertEqual(outcome.verdict.state, STATE_ACTIONABLE)

    def test_execute_retry_uses_marker_to_find_same_transaction_close_proof(self):
        ops = FakeOps()
        _preflight, fields = _authorize(ops)
        # First read cannot prove absence without an action id.  After the structured marker
        # supplies the exact action id, the adapter may consult that transaction and prove it.
        ops.current = _observation(slots=(_slot("gateway", locator=""), _slot("worker")))
        ops.action_current = _observation(
            slots=(_slot("gateway", proof=True), _slot("worker"))
        )
        ops.markers = [fields]
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.state, STATE_ALREADY_CONVERGED)
        self.assertTrue(any(call[0] == "drive" for call in ops.calls))

    def test_preserved_productive_pending_foreign_or_unknown_slot_blocks(self):
        ops = FakeOps(_observation(slots=(_slot("gateway", disposition=SLOT_PRESERVE_AMBIGUOUS), _slot("worker"))))
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_PAIR_PRESERVED)

    def test_execute_requires_a_live_structured_marker(self):
        ops = FakeOps()
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_APPROVAL_MISSING)
        self.assertFalse(any(call[0] == "drive" for call in ops.calls))

    def test_stale_or_mismatched_approval_is_zero_effect(self):
        ops = FakeOps()
        _preflight, fields = _authorize(ops)
        ops.markers = [{**fields, "revision": "5"}]
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_APPROVAL_MISMATCH)
        self.assertFalse(any(call[0] == "drive" for call in ops.calls))

    def test_replacement_stop_never_attempts_pin_cas(self):
        ops = FakeOps()
        _authorize(ops)
        ops.drive = ReplacementDrive(False, "preservation_blocked", "dirty_diff")
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_REPLACEMENT_STOPPED)
        self.assertFalse(any(call[0] == "repair" for call in ops.calls))

    def test_final_pair_must_be_both_healthy_and_locator_bound(self):
        ops = FakeOps()
        _authorize(ops)
        ops.final = _observation(slots=(_slot("gateway", disposition=SLOT_HEALTHY, locator="w2:p1"), _slot("worker", locator="")))
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_FRESH_PAIR_UNPROVEN)
        self.assertFalse(any(call[0] == "repair" for call in ops.calls))

    def test_pin_cas_refusal_leaves_transaction_replayable(self):
        ops = FakeOps()
        _authorize(ops)
        ops.repair = PinRepairResult(False, "stale_revision")
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.reason, BLOCK_PIN_CAS_REFUSED)
        self.assertFalse(any(call[0] == "finish" for call in ops.calls))

    def test_success_repairs_pins_completes_transaction_and_never_sends_or_resumes(self):
        ops = FakeOps()
        _authorize(ops)
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.state, STATE_ALREADY_CONVERGED)
        self.assertTrue(outcome.pins_repaired)
        self.assertEqual([call[0] for call in ops.calls[-4:]], ["drive", "final", "repair", "finish"])

    def test_already_converged_is_no_effect_success(self):
        healthy = (_slot("gateway", disposition=SLOT_HEALTHY), _slot("worker", disposition=SLOT_HEALTHY))
        ops = FakeOps(_observation(pins_empty=False, pins_exact=True, slots=healthy))
        outcome = run_bound_pair_convergence(REQ, execute=True, ops=ops)
        self.assertEqual(outcome.verdict.state, STATE_ALREADY_CONVERGED)
        self.assertEqual(ops.calls, [("observe", "")])


if __name__ == "__main__":
    unittest.main()
