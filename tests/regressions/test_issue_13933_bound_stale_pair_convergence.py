"""Regression matrix for #13933's bounded hibernated pair convergence rail."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
    ReplacementActionBindingError,
    herdr_identity_replacement_binding_path,
    replacement_action_is_bound,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    AttestationStoreLockBusy,
    attestation_store_lock,
)
from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer, ProcessGenerationPin
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E501
    V1_BINDING_STARTUP_DEBT,
    V1_BINDING_STARTUP_ROLLBACK_REQUIRED,
    V1ReplacementBindingFailure,
    launch_or_resume_v1_replacement,
)
from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    assess_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (
    ContinuationPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PHASE_COMPLETED,
)
from mozyo_bridge.core.state.workspace_registry import read_anchor
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
    _launch_detail,
    LiveBoundPairConvergenceOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    sublane_hibernated_bound_pair_convergence_live as CL,
    sublane_prepare_readonly_projection as PRP,
    sublane_quarantine as QM,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PrepareBoundPairRequest,
    PreparationObservation,
    STATE_BLOCKED,
    run_bound_pair_preparation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard_live import (
    LiveBoundPairPreparationOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (
    RESUME_STARTUP_ROLLBACK_REQUIRED,
    expectation_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    ACTUATION_EFFECT_FAILED,
    ACTUATION_PRESERVATION_BLOCKED,
    ACTUATION_RECOVERED,
    LAUNCH_ERROR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    decode_assigned_name,
    encode_assigned_name,
)

from tests.support.agent_provider_binaries import with_provider_path
from tests.support.herdr_fake import FakeHerdr
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    SublaneLauncherIncompatibleError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (
    HEAL_REASON_TARGET_ABSENT,
    SublaneHealError,
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

            def heal_lane_column(self, worktree, *, target_provider=None):
                calls.append(("heal", worktree, target_provider))

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.HerdrSublaneActuatorOps"
        )
        with mock.patch(module, FakeActuator):
            result = port.launch_action_bound("action-13933", self._pin())
        self.assertEqual(result, LAUNCH_DONE)
        self.assertEqual(calls[0][1]["replacement_action_id"], "action-13933")
        # Redmine #13933 R11 j#81429 #3: the launch is scoped to THIS owed participant's
        # provider so the pair-level launcher's same-tab postcondition converges an
        # approved partial pair instead of fencing on the still-owed / absent sibling.
        self.assertEqual(calls[1], ("heal", REQ.worktree, "codex"))
        # A clean launch records no failure reason (j#81429 #2).
        self.assertEqual(getattr(port, "launch_failure_reason", ""), "")

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


class LaunchReasonSurfacingTests(unittest.TestCase):
    """The typed heal reason is captured by the port and surfaced publicly (j#81429 #2)."""

    def _port(self):
        owner = SimpleNamespace(repo_root=Path("/coordinator"), env={})
        live = _SnapshotRecoveryOps(
            repo_root=owner.repo_root, request_issue=REQ.issue,
            request_lane=REQ.lane, request_journal=REQ.journal, env={},
        )
        return _BoundPairActuatorPort(owner, REQ, object(), live)

    def _pin(self):
        return ParticipantPin(
            lane_id=REQ.lane, role="worker", provider="claude",
            assigned_name="managed-worker", old_locator="w1:p2",
            is_self=False, lane_revision="4", lane_generation="1",
        )

    def _launch_with(self, raiser):
        port = self._port()

        class FakeActuator:
            def __init__(self, **kwargs):
                pass

            def heal_lane_column(self, worktree, *, target_provider=None):
                raiser()

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.HerdrSublaneActuatorOps"
        )
        with mock.patch(module, FakeActuator):
            result = port.launch_action_bound("action-13933", self._pin())
        return result, port

    def test_heal_error_reason_is_captured_not_swallowed(self):
        def raiser():
            raise SublaneHealError("boom", reason=HEAL_REASON_TARGET_ABSENT)

        result, port = self._launch_with(raiser)
        self.assertEqual(result, LAUNCH_ERROR)
        self.assertEqual(port.launch_failure_reason, HEAL_REASON_TARGET_ABSENT)

    def test_launcher_incompatible_reason_is_captured(self):
        def raiser():
            raise SublaneLauncherIncompatibleError("skew", reason="launcher_runtime_incompatible")

        result, port = self._launch_with(raiser)
        self.assertEqual(result, LAUNCH_ERROR)
        self.assertEqual(port.launch_failure_reason, "launcher_runtime_incompatible")

    def test_unexpected_exception_is_a_stable_generic_reason(self):
        def raiser():
            raise RuntimeError("some preflight fence (never a raw path)")

        result, port = self._launch_with(raiser)
        self.assertEqual(result, LAUNCH_ERROR)
        # A stable token, never the raw message (no path / credential leak).
        self.assertEqual(port.launch_failure_reason, "launch_error")

    def test_launch_detail_surfaces_typed_reason_only_for_launch_effect_failed(self):
        port = SimpleNamespace(launch_failure_reason=HEAL_REASON_TARGET_ABSENT)
        launch_failed = SimpleNamespace(
            status=ACTUATION_EFFECT_FAILED, detail="launch", preservation_reasons=()
        )
        self.assertEqual(
            _launch_detail(launch_failed, port), f"launch:{HEAL_REASON_TARGET_ABSENT}"
        )
        # A different effect_failed leg (e.g. a close) is NOT relabelled.
        close_failed = SimpleNamespace(
            status=ACTUATION_EFFECT_FAILED, detail="close", preservation_reasons=()
        )
        self.assertEqual(_launch_detail(close_failed, port), "close")
        # A preservation block keeps its own reasons.
        blocked = SimpleNamespace(
            status=ACTUATION_PRESERVATION_BLOCKED, detail="", preservation_reasons=("dirty",)
        )
        self.assertEqual(_launch_detail(blocked, port), "dirty")

    def test_launch_detail_without_a_captured_reason_stays_bare_launch(self):
        port = SimpleNamespace(launch_failure_reason="")
        launch_failed = SimpleNamespace(
            status=ACTUATION_EFFECT_FAILED, detail="launch", preservation_reasons=()
        )
        self.assertEqual(_launch_detail(launch_failed, port), "launch")


HERDR_ENV = "MOZYO_HERDR_BINARY"

_V1_ATTESTATION_DDL = (
    "CREATE TABLE herdr_identity_attestations ("
    "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, role TEXT NOT NULL, "
    "lane_id TEXT NOT NULL, locator TEXT NOT NULL, verdict TEXT NOT NULL, "
    "detail TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL)"
)


def _seed_v1_attestation_store(home: Path) -> Path:
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version=1")
        conn.execute(_V1_ATTESTATION_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


def _fake_binary(tmp: str) -> Path:
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


def _fake_attest_launcher(tmp: str) -> Path:
    launcher = Path(tmp) / "fake-mozyo-bridge"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(
        launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    return launcher


def _agent_list_rows(fake: FakeHerdr):
    import json

    out = fake.run(["herdr", "agent", "list"]).stdout
    return json.loads(out).get("agents", [])


def _append_v1_lane(tmp: str, *, lane: str, issue: str):
    """Seed a v1 home + registered lane workspace with a live, normal-v1-attested pair."""
    home = Path(tmp) / "home"
    home.mkdir()
    _seed_v1_attestation_store(home)
    coord = Path(tmp) / "coord"
    coord.mkdir()
    worktree = Path(tmp) / "lane-wt"
    worktree.mkdir()
    env = with_provider_path(
        {
            HERDR_ENV: str(_fake_binary(tmp)),
            "MOZYO_BRIDGE_HOME": str(home),
            "MOZYO_BRIDGE_LAUNCHER": str(_fake_attest_launcher(tmp)),
        }
    )
    fake = _V1AttestingHerdr(attestation_home=home)
    with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
        HerdrSublaneActuatorOps(
            repo_root=coord, lane_label=lane, issue=issue, env=env, runner=fake.run,
        ).append_lane_column(str(worktree))
    ws = read_anchor(worktree)["workspace_id"]
    gw_name = encode_assigned_name(ws, "codex", lane)
    wk_name = encode_assigned_name(ws, "claude", lane)
    gw_old = fake.agent_named(gw_name)["pane_id"]
    wk_old = fake.agent_named(wk_name)["pane_id"]
    return home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old


class _AttestingHerdr(FakeHerdr):
    """FakeHerdr that self-attests each real launch and can drop one launch of a provider.

    On a successful ``agent start`` it upserts the launched slot's startup self-attestation
    bound to ``action_id`` (what the #13637 attest wrapper would write live — the fake runs no
    wrapper). ``fail_launch_provider`` makes the NEXT ``agent start`` of that provider return an
    ``agent_started`` envelope WITHOUT a live row, so the real heal's same-tab postcondition
    fences on the absent target (``launch_target_absent``) — a real launch failure, not a
    scripted one.
    """

    def __init__(self, *, attestation_home: Path, **kw):
        super().__init__(**kw)
        self._attestation_home = attestation_home
        self.action_id = ""
        self.fail_launch_provider = ""

    def _cmd_agent_start(self, argv, rest):
        import json

        name = rest[2] if len(rest) > 2 else ""
        decoded = decode_assigned_name(name)
        provider = decoded.identity.role if (decoded.ok and decoded.identity) else ""
        if self.fail_launch_provider and provider == self.fail_launch_provider:
            self.fail_launch_provider = ""
            wid = rest[rest.index("--workspace") + 1] if "--workspace" in rest else "w1"
            tab = rest[rest.index("--tab") + 1] if "--tab" in rest else ""
            return __import__("subprocess").CompletedProcess(
                argv, 0,
                stdout=json.dumps({"result": {"type": "agent_started", "agent": {
                    "name": name, "pane_id": f"{wid}:pX", "workspace_id": wid, "tab_id": tab}}}),
                stderr="",
            )
        result = super()._cmd_agent_start(argv, rest)
        live = self.agent_named(name)
        if live and self.action_id and decoded.ok and decoded.identity is not None:
            HerdrIdentityAttestationStore(home=self._attestation_home).upsert(
                IdentityAttestationRecord(
                    assigned_name=name,
                    workspace_id=decoded.identity.workspace_id,
                    role=decoded.identity.role,
                    lane_id=decoded.identity.lane_id,
                    locator=live["pane_id"],
                    verdict=VERDICT_PRESENT,
                    observed_at="2099-07-18T00:00:00+00:00",
                    replacement_action_id=self.action_id,
                )
            )
        return result


class _V1AttestingHerdr(_AttestingHerdr):
    """The installed mixed-runtime shape: healthy launch, normal v1 row, no action field."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.start_calls: list[str] = []
        self.start_hook = None
        #: Providers whose fresh launch lands a live row but WITHOUT a self-attestation, so
        #: the real health probe reports the launched target non-green (Redmine #13933 R13
        #: scenario B: the target itself, not an adopted sibling, failed to come up).
        self.skip_attestation_for: set[str] = set()

    def run(self, argv, *args, **kwargs):
        if list(argv[1:]) == ["herdr", "agent-attest", "--help"]:
            return __import__("subprocess").CompletedProcess(
                argv,
                0,
                stdout=(
                    "usage: mozyo-bridge herdr agent-attest --assigned-name NAME\n"
                    "mozyo_attest_capability_schema=2\n"
                    "mozyo_attest_capability_stores=1_2\n"
                ),
                stderr="",
            )
        return super().run(argv, *args, **kwargs)

    def _cmd_agent_start(self, argv, rest):
        self.start_calls.append(rest[2] if len(rest) > 2 else "")
        if self.start_hook is not None:
            self.start_hook()
        desired_action = self.action_id
        self.action_id = ""
        try:
            result = super()._cmd_agent_start(argv, rest)
        finally:
            self.action_id = desired_action
        name = rest[2] if len(rest) > 2 else ""
        decoded = decode_assigned_name(name)
        live = self.agent_named(name)
        provider = decoded.identity.role if (decoded.ok and decoded.identity) else ""
        if provider in self.skip_attestation_for:
            # A launched-but-non-green target: the live row exists (so the slot is
            # ``launched`` with a locator), yet no self-attestation lands, so the real probe
            # reports it non-green and the fresh launch owes a rollback (scenario B).
            return result
        if live and decoded.ok and decoded.identity is not None:
            HerdrIdentityAttestationStore(home=self._attestation_home).upsert(
                IdentityAttestationRecord(
                    assigned_name=name,
                    workspace_id=decoded.identity.workspace_id,
                    role=decoded.identity.role,
                    lane_id=decoded.identity.lane_id,
                    locator=live["pane_id"],
                    verdict=VERDICT_PRESENT,
                    observed_at="2099-07-18T00:00:00+00:00",
                    replacement_action_id="",
                )
            )
        return result


class _CleanPreservationPort(_BoundPairActuatorPort):
    """The real convergence port with ONLY the read-only close-boundary observation modeled.

    Reconstructing an exact bound lifecycle row + a colocated git-worktree identity purely so a
    READ-ONLY :meth:`observe_preservation` clears is disproportionate; the pure
    :func:`assess_preservation` policy still decides, and every EFFECT leg — the real quarantine
    close, the real ``heal_lane_column`` launch, the real attestation-store verify, the real
    transaction + completion — runs unmodified against the live fake state.
    """

    def observe_preservation(self, pin):
        return PreservationObservation(identity_matches=True, attestation_fresh=True)


class V1ReplacementBindingStoreTests(unittest.TestCase):
    ACTION = "conv-v1-action-1"
    NAME = "mzb1_ws1_claude_lane1"

    def _reserve(self, home: Path, **over):
        values = dict(
            action_id=self.ACTION,
            assigned_name=self.NAME,
            workspace_id="ws1",
            role="claude",
            lane_id="lane1",
            old_locator="w1:p-old",
            startup_nonce="nonce-1",
            startup_action_id="startup-1",
        )
        values.update(over)
        return HerdrIdentityReplacementBindingStore(home=home).reserve(**values)

    def test_exact_binding_is_idempotent_and_generation_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1_attestation_store(home)
            store = HerdrIdentityReplacementBindingStore(home=home)
            intent = self._reserve(home)
            self.assertEqual(self._reserve(home), intent)
            record = HerdrIdentityAttestationStore(home=home).upsert(
                IdentityAttestationRecord(
                    assigned_name=self.NAME,
                    workspace_id="ws1",
                    role="claude",
                    lane_id="lane1",
                    locator="w1:p-new",
                    verdict=VERDICT_PRESENT,
                    observed_at="2099-07-18T00:00:00+00:00",
                )
            )
            bound = store.bind(
                intent,
                attestation=record,
                receipt_startup_action_id="startup-1",
                receipt_role="claude",
                receipt_assigned_name=self.NAME,
                receipt_locator="w1:p-new",
                receipt_present=True,
            )
            self.assertEqual(store.bind(
                intent,
                attestation=record,
                receipt_startup_action_id="startup-1",
                receipt_role="claude",
                receipt_assigned_name=self.NAME,
                receipt_locator="w1:p-new",
                receipt_present=True,
            ), bound)
            check = dict(
                action_id=self.ACTION,
                live_locator="w1:p-new",
                expected_workspace_id="ws1",
                expected_role="claude",
                expected_lane="lane1",
                expected_assigned_name=self.NAME,
                expected_old_locator="w1:p-old",
                home=home,
            )
            self.assertTrue(replacement_action_is_bound(record, **check))
            for key, foreign in (
                ("action_id", "foreign-action"),
                ("live_locator", "w1:p-other"),
                ("expected_workspace_id", "foreign-ws"),
                ("expected_role", "codex"),
                ("expected_lane", "foreign-lane"),
                ("expected_assigned_name", "foreign-name"),
                ("expected_old_locator", "w1:p-foreign-old"),
            ):
                candidate = dict(check)
                candidate[key] = foreign
                self.assertFalse(replacement_action_is_bound(record, **candidate), key)

    def test_foreign_attempt_and_unsafe_file_shape_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1_attestation_store(home)
            self._reserve(home)
            with self.assertRaises(ReplacementActionBindingError):
                self._reserve(home, startup_nonce="nonce-foreign")
            with self.assertRaises(ReplacementActionBindingError):
                self._reserve(Path(tmp) / "empty-old", old_locator="")

            path = herdr_identity_replacement_binding_path(home)
            os.chmod(path, 0o644)
            with self.assertRaises(ReplacementActionBindingError):
                HerdrIdentityReplacementBindingStore(home=home).read(
                    self.ACTION, self.NAME
                )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_unknown_schema_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1_attestation_store(home)
            self._reserve(home)
            path = herdr_identity_replacement_binding_path(home)
            conn = sqlite3.connect(path)
            try:
                conn.execute("ALTER TABLE replacement_action_bindings ADD COLUMN junk TEXT")
                conn.commit()
            finally:
                conn.close()
            before = path.read_bytes()
            with self.assertRaises(ReplacementActionBindingError):
                HerdrIdentityReplacementBindingStore(home=home).read(
                    self.ACTION, self.NAME
                )
            self.assertEqual(path.read_bytes(), before)


class RealLauncherCompositionTests(unittest.TestCase):
    """Redmine #13933 R11 F1 (review j#81456): the REAL launcher composes end to end.

    One fixture drives the REAL :meth:`HerdrSublaneActuatorOps.heal_lane_column` (a shared
    ``FakeHerdr`` at the subprocess Runner boundary — NOT a counting launcher) through the REAL
    :class:`_BoundPairActuatorPort` + REAL :class:`ReplacementTransactionStore` +
    ``ReplacementActuatorUseCase.drive_worker_recovery`` + the production completion leg
    (:meth:`LiveBoundPairConvergenceOps.finish_replacement`). It excludes the installed-a8
    (j#81426) failure class "converges under a test double but stalls at the real launcher
    composition" that three separately-green layers could not.

    Shape = the exact #13846 partial pair: gateway absent, worker stale-live-unattested. Run 1
    closes the worker for real, then its heal launch fails (the real same-tab postcondition
    fences on the target that did not come up -> ``launch_target_absent``). Run 2 replays the
    SAME immutable action: never re-closes, relaunches, and BOTH roles reach a fresh locator +
    an ``action_id``-bound startup attestation; the transaction reaches ``PHASE_COMPLETED``.
    """

    ACTION = "conv-real-act-1"
    LANE = "issue_13846_real_conv"
    ISSUE = "13846"
    FIXED = "2099-07-18T12:00:00+00:00"

    def _drive(self, use_case_key, holder):
        store, key, port = use_case_key
        return ReplacementActuatorUseCase(
            store, port, preservation_policy=assess_preservation, clock=lambda: self.FIXED,
        ).drive_worker_recovery(key, holder=holder, expected_action_generation=1)

    def test_real_heal_composes_partial_convergence_replay_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            coord = Path(tmp) / "coord"; coord.mkdir()
            worktree = Path(tmp) / "lane-wt"; worktree.mkdir()
            env = with_provider_path(
                {HERDR_ENV: str(_fake_binary(tmp)), "MOZYO_BRIDGE_HOME": str(home)}
            )
            fake = _AttestingHerdr(attestation_home=home)

            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # Real append mints the lane workspace + a live (unattested) gateway/worker pair.
                HerdrSublaneActuatorOps(
                    repo_root=coord, lane_label=self.LANE, issue=self.ISSUE,
                    env=env, runner=fake.run,
                ).append_lane_column(str(worktree))
                ws = read_anchor(worktree)["workspace_id"]
                gw_name = encode_assigned_name(ws, "codex", self.LANE)
                wk_name = encode_assigned_name(ws, "claude", self.LANE)
                gw_old = fake.agent_named(gw_name)["pane_id"]
                wk_old = fake.agent_named(wk_name)["pane_id"]

                # Partial shape: gateway absent, worker stale-live.
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]
                fake.action_id = self.ACTION

                store = ReplacementTransactionStore(home=home)
                key = ReplacementTransactionKey(ws, self.ACTION)
                gw_pin = ParticipantPin(
                    lane_id=self.LANE, role="gateway", provider="codex",
                    assigned_name=gw_name, old_locator=gw_old,
                    lane_revision="1", lane_generation="1",
                )
                wk_pin = ParticipantPin(
                    lane_id=self.LANE, role="worker", provider="claude",
                    assigned_name=wk_name, old_locator=wk_old,
                    lane_revision="1", lane_generation="1",
                )
                store.plan_transaction(
                    key, action_generation=1,
                    decision=DecisionPointer(source="redmine", issue_id=self.ISSUE, journal_id="80925"),
                    continuation=ContinuationPointer(
                        source="redmine", issue_id=self.ISSUE, journal_id="80925",
                        expected_gate="bound_pair_convergence_approval",
                        next_semantic_action="repair_pins",
                    ),
                    participants=[gw_pin, wk_pin],
                )

                owner = LiveBoundPairConvergenceOps(
                    repo_root=coord, env=env, transaction_store=store
                )
                request = ConvergeBoundPairRequest(
                    issue=self.ISSUE, journal="80925", lane=self.LANE,
                    worktree=str(worktree), branch="main",
                )
                # The live ops resolve the quarantine-close workspace from the worktree (the
                # lane worktree inherits the project workspace in production); the fake runner
                # drives its close + heal.
                live = _SnapshotRecoveryOps(
                    repo_root=Path(str(worktree)), request_issue=self.ISSUE,
                    request_lane=self.LANE, request_journal="80925", env=env, runner=fake.run,
                )
                live.target_workspace_id = ws
                expectation = SimpleNamespace(action_id=self.ACTION, action_generation=1)
                port = _CleanPreservationPort(owner, request, expectation, live)
                holder = f"converge:{self.ACTION}:g1"

                closes: list = []
                orig_close = QM.LiveSublaneQuarantineOps.close_receiver

                def counting_close(ops_self, req, pin):
                    res = orig_close(ops_self, req, pin)
                    if res.closed:
                        closes.append(pin.locator)
                    return res

                def refresh_and_drive():
                    live.snapshot_rows = tuple(_agent_list_rows(fake))
                    return self._drive((store, key, port), holder)

                with mock.patch.object(CL, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)), \
                     mock.patch.object(QM, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)), \
                     mock.patch.object(QM.LiveSublaneQuarantineOps, "close_receiver", counting_close), \
                     mock.patch.object(
                         CL, "HerdrSublaneActuatorOps",
                         lambda **kw: HerdrSublaneActuatorOps(**kw, runner=fake.run),
                     ):
                    # Run 1: the worker closes for real, then its heal launch fails.
                    fake.fail_launch_provider = "claude"
                    run1 = refresh_and_drive()
                    self.assertEqual(run1.status, ACTUATION_EFFECT_FAILED)
                    # The real same-tab postcondition raised a typed reason the real port captured.
                    self.assertEqual(port.launch_failure_reason, "launch_target_absent")
                    self.assertEqual(
                        store.get(key).find_participant(wk_pin.identity).phase,
                        PARTICIPANT_LAUNCH_OWED,
                    )
                    self.assertEqual(closes, [wk_old])  # the worker was closed exactly once

                    # Run 2: same immutable action; launch now succeeds.
                    run2 = refresh_and_drive()
                    self.assertEqual(run2.status, ACTUATION_RECOVERED)
                    self.assertEqual(closes, [wk_old])  # NEVER re-closed on the replay

                    completed = owner.finish_replacement(expectation)

                # Both roles converged to `replaced`.
                for pin in (gw_pin, wk_pin):
                    self.assertEqual(
                        store.get(key).find_participant(pin.identity).phase,
                        PARTICIPANT_REPLACED,
                        pin.role,
                    )
                # Both roles are live at a FRESH locator with an action-bound attestation.
                attest_store = HerdrIdentityAttestationStore(home=home)
                for name, old in ((gw_name, gw_old), (wk_name, wk_old)):
                    live_row = fake.agent_named(name)
                    self.assertIsNotNone(live_row)
                    self.assertNotEqual(live_row["pane_id"], old)  # fresh generation
                    record = attest_store.read(name)
                    self.assertEqual(record.replacement_action_id, self.ACTION)
                    self.assertEqual(record.locator, live_row["pane_id"])
                # The production completion leg reached PHASE_COMPLETED.
                self.assertTrue(completed)
                self.assertEqual(store.get(key).phase, PHASE_COMPLETED)

    def _exercise_v1_side_binding(self, *, fail_first_bind: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            _seed_v1_attestation_store(home)
            coord = Path(tmp) / "coord"
            coord.mkdir()
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            env = with_provider_path(
                {
                    HERDR_ENV: str(_fake_binary(tmp)),
                    "MOZYO_BRIDGE_HOME": str(home),
                    "MOZYO_BRIDGE_LAUNCHER": str(_fake_attest_launcher(tmp)),
                }
            )
            fake = _V1AttestingHerdr(attestation_home=home)

            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                HerdrSublaneActuatorOps(
                    repo_root=coord,
                    lane_label=self.LANE,
                    issue=self.ISSUE,
                    env=env,
                    runner=fake.run,
                ).append_lane_column(str(worktree))
                ws = read_anchor(worktree)["workspace_id"]
                gw_name = encode_assigned_name(ws, "codex", self.LANE)
                wk_name = encode_assigned_name(ws, "claude", self.LANE)
                gw_old = fake.agent_named(gw_name)["pane_id"]
                wk_old = fake.agent_named(wk_name)["pane_id"]

                # A normal-v1 live generation with no reserve-before-launch side binding
                # is foreign to a replacement action.  Never adopt/fabricate a binding.
                with self.assertRaises(SublaneHealError) as unbound:
                    HerdrSublaneActuatorOps(
                        repo_root=coord,
                        lane_label=self.LANE,
                        issue=self.ISSUE,
                        journal="80925",
                        env=env,
                        runner=fake.run,
                        replacement_action_id="foreign-action",
                        replacement_assigned_name=gw_name,
                        replacement_old_locator=gw_old,
                    ).heal_lane_column(str(worktree), target_provider="codex")
                self.assertEqual(
                    unbound.exception.reason,
                    "replacement_binding_authority_conflict",
                )
                self.assertEqual(len(fake.start_calls), 2)
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]

                maintenance_attempts: list[str] = []

                def assert_generation_is_pinned():
                    try:
                        with attestation_store_lock(
                            home, exclusive=True, blocking=False
                        ):
                            maintenance_attempts.append("unexpectedly_acquired")
                    except AttestationStoreLockBusy:
                        maintenance_attempts.append("busy")

                fake.start_hook = assert_generation_is_pinned

                store = ReplacementTransactionStore(home=home)
                key = ReplacementTransactionKey(ws, self.ACTION)
                gw_pin = ParticipantPin(
                    lane_id=self.LANE,
                    role="gateway",
                    provider="codex",
                    assigned_name=gw_name,
                    old_locator=gw_old,
                    lane_revision="1",
                    lane_generation="1",
                )
                wk_pin = ParticipantPin(
                    lane_id=self.LANE,
                    role="worker",
                    provider="claude",
                    assigned_name=wk_name,
                    old_locator=wk_old,
                    lane_revision="1",
                    lane_generation="1",
                )
                store.plan_transaction(
                    key,
                    action_generation=1,
                    decision=DecisionPointer(
                        source="redmine", issue_id=self.ISSUE, journal_id="80925"
                    ),
                    continuation=ContinuationPointer(
                        source="redmine",
                        issue_id=self.ISSUE,
                        journal_id="80925",
                        expected_gate="bound_pair_convergence_approval",
                        next_semantic_action="repair_pins",
                    ),
                    participants=[gw_pin, wk_pin],
                )
                owner = LiveBoundPairConvergenceOps(
                    repo_root=coord, env=env, transaction_store=store
                )
                request = ConvergeBoundPairRequest(
                    issue=self.ISSUE,
                    journal="80925",
                    lane=self.LANE,
                    worktree=str(worktree),
                    branch="main",
                )
                live = _SnapshotRecoveryOps(
                    repo_root=Path(str(worktree)),
                    request_issue=self.ISSUE,
                    request_lane=self.LANE,
                    request_journal="80925",
                    env=env,
                    runner=fake.run,
                )
                live.target_workspace_id = ws
                expectation = SimpleNamespace(action_id=self.ACTION, action_generation=1)
                port = _CleanPreservationPort(owner, request, expectation, live)
                holder = f"converge:{self.ACTION}:g1"

                def refresh_and_drive():
                    live.snapshot_rows = tuple(_agent_list_rows(fake))
                    return self._drive((store, key, port), holder)

                real_bind = HerdrIdentityReplacementBindingStore.bind
                bind_calls = 0

                def maybe_fail_bind(binding_self, *args, **kwargs):
                    nonlocal bind_calls
                    bind_calls += 1
                    if fail_first_bind and bind_calls == 1:
                        raise ReplacementActionBindingError("simulated bind interruption")
                    return real_bind(binding_self, *args, **kwargs)

                with mock.patch.object(
                    CL, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)
                ), mock.patch.object(
                    QM, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)
                ), mock.patch.object(
                    CL,
                    "HerdrSublaneActuatorOps",
                    lambda **kw: HerdrSublaneActuatorOps(**kw, runner=fake.run),
                ), mock.patch.object(
                    HerdrIdentityReplacementBindingStore, "bind", maybe_fail_bind
                ):
                    first = refresh_and_drive()
                    if fail_first_bind:
                        self.assertEqual(first.status, ACTUATION_EFFECT_FAILED)
                        self.assertEqual(
                            port.launch_failure_reason,
                            "replacement_binding_authority_conflict",
                        )
                        second = refresh_and_drive()
                        self.assertEqual(second.status, ACTUATION_RECOVERED)
                    else:
                        self.assertEqual(first.status, ACTUATION_RECOVERED)

                # Initial pair + one fresh launch per role.  Replaying an interrupted
                # side bind resumes the exact startup receipt; it never relaunches the
                # already-live gateway.
                self.assertEqual(len(fake.start_calls), 4)
                self.assertEqual(maintenance_attempts, ["busy", "busy"])
                attestation_store = HerdrIdentityAttestationStore(home=home)
                for pin in (gw_pin, wk_pin):
                    live_row = fake.agent_named(pin.assigned_name)
                    self.assertIsNotNone(live_row)
                    record = attestation_store.read(pin.assigned_name)
                    self.assertEqual(record.replacement_action_id, "")
                    self.assertTrue(
                        replacement_action_is_bound(
                            record,
                            action_id=self.ACTION,
                            live_locator=live_row["pane_id"],
                            expected_workspace_id=ws,
                            expected_role=pin.provider,
                            expected_lane=self.LANE,
                            expected_assigned_name=pin.assigned_name,
                            expected_old_locator=pin.old_locator,
                            home=home,
                        )
                    )
                    self.assertEqual(
                        store.get(key).find_participant(pin.identity).phase,
                        PARTICIPANT_REPLACED,
                    )
                self.assertTrue(owner.finish_replacement(expectation))
                self.assertEqual(store.get(key).phase, PHASE_COMPLETED)
                with sqlite3.connect(herdr_identity_attestation_path(home)) as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_real_v1_store_uses_exact_side_binding_without_migration(self):
        self._exercise_v1_side_binding(fail_first_bind=False)

    def test_real_v1_store_replays_interrupted_bind_without_relaunch(self):
        self._exercise_v1_side_binding(fail_first_bind=True)


class PartialStartupBindingHealthTests(unittest.TestCase):
    """Redmine #13933 R13 (installed a14 j#82038): the v1 replacement bind settles its debt
    on the EXACT target participant's own health, never the managed-pair aggregate.

    The installed a14 failure was a single-leg gateway replacement whose fresh target came up
    healthy (new productive locator) while the OLD worker it did not launch was a non-green
    pending sibling. ``result.ok`` (the pair aggregate) was therefore false, so the bind
    stopped ``replacement_binding_launch_unhealthy`` and the outer participant stranded at
    ``launch_owed`` — a healthy target the tool refused to bind. These tests drive the REAL
    ``heal_lane_column`` -> real ``prepare_session`` -> real startup transaction + v1 binding
    store composition (a ``FakeHerdr`` only at the subprocess boundary), separating an adopted
    sibling's non-green from the target's own.
    """

    LANE = "issue_13846_partial_bind"
    ISSUE = "13846"
    ACTION = "partial-bind-act-1"

    def _append_v1_pair(self, tmp: str):
        return _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)

    def _heal_gateway(self, home, coord, worktree, env, fake, gw_name, gw_old):
        ops = HerdrSublaneActuatorOps(
            repo_root=coord, lane_label=self.LANE, issue=self.ISSUE, journal="80925",
            env=env, runner=fake.run,
            replacement_action_id=self.ACTION,
            replacement_assigned_name=gw_name,
            replacement_old_locator=gw_old,
        )
        with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            return ops.heal_lane_column(str(worktree), target_provider="codex")

    def _startup_phase(self, home, ws) -> str:
        intent = HerdrIdentityReplacementBindingStore(home=home).read(
            self.ACTION, encode_assigned_name(ws, "codex", self.LANE)
        )
        self.assertIsNotNone(intent)
        action = StartupTransactionFence(home=home).read(intent.startup_action_id)
        self.assertIsNotNone(action)
        return action.phase

    def test_healthy_target_binds_despite_a_non_green_adopted_sibling(self):
        # Scenario A (the exact installed a14 shape): gateway absent -> fresh healthy target;
        # the worker it did not launch is live-but-unattested (a non-green pending sibling).
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                self._append_v1_pair(tmp)
            )
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # Strip the worker's self-attestation so the real probe surfaces it non-green
                # (unattested), and remove the gateway so it is the fresh replacement target.
                HerdrIdentityAttestationStore(home=home).upsert(
                    IdentityAttestationRecord(
                        assigned_name=wk_name, workspace_id=ws, role="claude",
                        lane_id=self.LANE, locator="w0:pGHOST", verdict=VERDICT_PRESENT,
                        observed_at="2099-07-18T00:00:00+00:00", replacement_action_id="",
                    )
                )
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]

                # The bind must SUCCEED on the healthy target — no aggregate stop.
                self._heal_gateway(home, coord, worktree, env, fake, gw_name, gw_old)

            # The target is live at a fresh locator, action-bound in the v1 store.
            gw_live = fake.agent_named(gw_name)
            self.assertIsNotNone(gw_live)
            self.assertNotEqual(gw_live["pane_id"], gw_old)
            record = HerdrIdentityAttestationStore(home=home).read(gw_name)
            self.assertTrue(
                replacement_action_is_bound(
                    record, action_id=self.ACTION, live_locator=gw_live["pane_id"],
                    expected_workspace_id=ws, expected_role="codex",
                    expected_lane=self.LANE, expected_assigned_name=gw_name,
                    expected_old_locator=gw_old, home=home,
                )
            )
            # The startup transaction settled SUCCESS: the only fresh launch was healthy, so
            # the run owed no rollback even though the surfaced sibling made the pair unusable.
            self.assertEqual(self._startup_phase(home, ws), PHASE_COMPLETED_SUCCESS)

    def test_a_non_green_target_fails_closed_and_owes_a_rollback(self):
        # Scenario B: gateway absent -> fresh target that lands a live locator but never
        # attests (non-green target). The worker is healthy/adopted, so the ONLY debt is the
        # target's own. The bind fails closed and leaves the a14 partial the rollback rail owns.
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                self._append_v1_pair(tmp)
            )
            fake.skip_attestation_for = {"codex"}
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]
                with self.assertRaises(SublaneHealError) as caught:
                    self._heal_gateway(home, coord, worktree, env, fake, gw_name, gw_old)
            self.assertEqual(
                caught.exception.reason, "replacement_binding_launch_unhealthy"
            )
            # The a14 partial the correction must own: reserved + a live locator + the startup
            # transaction durably rollback_owed (never silently promoted to success).
            gw_live = fake.agent_named(gw_name)
            self.assertIsNotNone(gw_live)
            self.assertNotEqual(gw_live["pane_id"], gw_old)
            self.assertEqual(self._startup_phase(home, ws), PHASE_ROLLBACK_OWED)
            # The target was launched but never bound: no action-bound v1 attestation row.
            record = HerdrIdentityAttestationStore(home=home).read(gw_name)
            self.assertFalse(
                replacement_action_is_bound(
                    record, action_id=self.ACTION, live_locator=gw_live["pane_id"],
                    expected_workspace_id=ws, expected_role="codex",
                    expected_lane=self.LANE, expected_assigned_name=gw_name,
                    expected_old_locator=gw_old, home=home,
                )
            )

    def test_same_action_replays_fresh_launch_after_a_public_rollback(self):
        # Item 4: once the public rollback rail has durably rolled the fresh launch back, the
        # SAME action resumes idempotently to a fresh relaunch + bind — never a blind retry of
        # the old attempt and never a raw rollback.
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                self._append_v1_pair(tmp)
            )
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # A prior attempt this action owns, durably rolled back by the public rail.
                nonce0 = "rolled-nonce-0"
                managed = ("claude", "codex")
                unit = StartupUnit(ws, self.LANE, managed)
                sa0 = startup_action_id(unit, nonce0)
                HerdrIdentityReplacementBindingStore(home=home).reserve(
                    action_id=self.ACTION, assigned_name=gw_name, workspace_id=ws,
                    role="codex", lane_id=self.LANE, old_locator=gw_old,
                    startup_nonce=nonce0, startup_action_id=sa0,
                )
                fence = StartupTransactionFence(home=home)
                fence.reserve(unit, nonce0)
                fence.record_participant(
                    sa0, Participant(role="codex", assigned_name=gw_name,
                                     locator="w1:pROLLED", receipt="workspace=w1")
                )
                fence.set_phase(sa0, PHASE_HEALTH_CHECK)
                fence.set_phase(sa0, PHASE_ROLLBACK_OWED)
                fence.mark_closed(sa0, "codex")
                fence.set_phase(sa0, PHASE_COMPLETED_ROLLED_BACK)

                # The rolled-back target is absent (the rollback closed it).
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]

                # Replay the SAME action: a fresh relaunch + bind, no re-use of the old attempt.
                self._heal_gateway(home, coord, worktree, env, fake, gw_name, gw_old)

            gw_live = fake.agent_named(gw_name)
            self.assertIsNotNone(gw_live)
            self.assertNotIn(gw_live["pane_id"], {gw_old, "w1:pROLLED"})  # a fresh generation
            record = HerdrIdentityAttestationStore(home=home).read(gw_name)
            self.assertTrue(
                replacement_action_is_bound(
                    record, action_id=self.ACTION, live_locator=gw_live["pane_id"],
                    expected_workspace_id=ws, expected_role="codex",
                    expected_lane=self.LANE, expected_assigned_name=gw_name,
                    expected_old_locator=gw_old, home=home,
                )
            )
            # The replay reached a NEW durable startup success (not the old rolled-back one).
            self.assertEqual(self._startup_phase(home, ws), PHASE_COMPLETED_SUCCESS)


class SeededA14PartialProjectionTests(unittest.TestCase):
    """Redmine #13933 R13 item 3: the durable a14 partial (reserved binding + a startup
    transaction the run left ``rollback_owed`` at an exact live locator) is projected as a
    typed ``action_owned_startup_rollback_required``, never silently bound or promoted — even
    when the slot now reads live + attested. Every conjunct must match; a mismatch preserves
    the generic fail-closed debt.
    """

    WS = "mzb1ws13846partial"
    LANE = "issue_13846_partial_bind"
    PROVIDER = "codex"
    ACTION = "seeded-a14-act-1"
    MANAGED = ("claude", "codex")

    def _seed(
        self, home: Path, *, receipt: str = "workspace=w1", attest_locator: str = "w1:pNEW",
        phase: str = PHASE_ROLLBACK_OWED, live_locator: str = "w1:pNEW",
    ):
        _seed_v1_attestation_store(home)
        gw_name = encode_assigned_name(self.WS, self.PROVIDER, self.LANE)
        gw_old = "w1:pOLD"
        nonce = "seed-nonce-1"
        unit = StartupUnit(self.WS, self.LANE, self.MANAGED)
        sa_id = startup_action_id(unit, nonce)
        HerdrIdentityReplacementBindingStore(home=home).reserve(
            action_id=self.ACTION, assigned_name=gw_name, workspace_id=self.WS,
            role=self.PROVIDER, lane_id=self.LANE, old_locator=gw_old,
            startup_nonce=nonce, startup_action_id=sa_id,
        )
        fence = StartupTransactionFence(home=home)
        fence.reserve(unit, nonce)
        fence.record_participant(
            sa_id,
            Participant(role=self.PROVIDER, assigned_name=gw_name,
                        locator=live_locator, receipt=receipt),
        )
        fence.set_phase(sa_id, PHASE_HEALTH_CHECK)
        fence.set_phase(sa_id, phase)
        if attest_locator:
            HerdrIdentityAttestationStore(home=home).upsert(
                IdentityAttestationRecord(
                    assigned_name=gw_name, workspace_id=self.WS, role=self.PROVIDER,
                    lane_id=self.LANE, locator=attest_locator, verdict=VERDICT_PRESENT,
                    observed_at="2099-07-18T00:00:00+00:00", replacement_action_id="",
                )
            )
        rows = [{"name": gw_name, "pane_id": live_locator, "workspace_id": self.WS}]
        existing = {self.PROVIDER: (live_locator, gw_name)}
        return gw_name, gw_old, rows, existing

    def _resume(self, home, gw_name, gw_old, rows, existing):
        def _must_not_launch(nonce, fence):
            raise AssertionError("a live-locator partial must never relaunch")

        launch_or_resume_v1_replacement(
            home=home, action_id=self.ACTION, assigned_name=gw_name, old_locator=gw_old,
            target_provider=self.PROVIDER, workspace_id=self.WS, lane_id=self.LANE,
            managed_pair=self.MANAGED, rows=rows, existing=existing, launch=_must_not_launch,
        )

    def test_seeded_rollback_owed_partial_projects_typed_rollback_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            gw_name, gw_old, rows, existing = self._seed(home)
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(V1ReplacementBindingFailure) as caught:
                    self._resume(home, gw_name, gw_old, rows, existing)
            self.assertEqual(
                caught.exception.reason, V1_BINDING_STARTUP_ROLLBACK_REQUIRED
            )
            # Value-safe: the public reason never leaks the locator / receipt bytes.
            self.assertNotIn("w1:pNEW", caught.exception.detail)
            self.assertNotIn("w1:pOLD", caught.exception.detail)

    def test_rollback_owed_partial_without_receipt_falls_through_to_debt(self):
        # A missing participant receipt is NOT the owned partial — it fails closed on the
        # generic debt, never the actionable rollback-required projection.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            gw_name, gw_old, rows, existing = self._seed(home, receipt="")
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(V1ReplacementBindingFailure) as caught:
                    self._resume(home, gw_name, gw_old, rows, existing)
            self.assertEqual(caught.exception.reason, V1_BINDING_STARTUP_DEBT)

    def test_rollback_owed_partial_without_attestation_falls_through_to_debt(self):
        # No exact normal-v1 attestation row -> not the clean owned partial -> generic debt.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            gw_name, gw_old, rows, existing = self._seed(home, attest_locator="")
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(V1ReplacementBindingFailure) as caught:
                    self._resume(home, gw_name, gw_old, rows, existing)
            self.assertEqual(caught.exception.reason, V1_BINDING_STARTUP_DEBT)


class A14PartialPreflightSurfaceTests(unittest.TestCase):
    """Redmine #13933 R13 F1 (review j#82079): the read-only ``prepare-bound-pair`` preflight
    both NAMES the durable a14 rollback-owed partial and hands the operator the exact inner
    startup ``--action-id`` the public rollback rail needs — through the public entry point,
    with the REAL cross-store detection (side binding + startup fence + attestation)."""

    LANE = "issue_13846_preflight_surface"
    ISSUE = "13846"

    def _seed_partial(
        self, home, ws, gw_name, gw_live, *, action, receipt="workspace=w1",
        phase=PHASE_ROLLBACK_OWED, old_locator="w1:pPREV", attest_locator=None,
    ):
        nonce = f"preflight-nonce-{action}"
        managed = ("claude", "codex")
        unit = StartupUnit(ws, self.LANE, managed)
        sa_id = startup_action_id(unit, nonce)
        HerdrIdentityReplacementBindingStore(home=home).reserve(
            action_id=action, assigned_name=gw_name, workspace_id=ws, role="codex",
            lane_id=self.LANE, old_locator=old_locator, startup_nonce=nonce,
            startup_action_id=sa_id,
        )
        fence = StartupTransactionFence(home=home)
        fence.reserve(unit, nonce)
        fence.record_participant(
            sa_id, Participant(role="codex", assigned_name=gw_name,
                               locator=gw_live, receipt=receipt)
        )
        fence.set_phase(sa_id, PHASE_HEALTH_CHECK)
        fence.set_phase(sa_id, phase)
        if attest_locator is not None:  # override the append's clean normal-v1 row
            HerdrIdentityAttestationStore(home=home).upsert(
                IdentityAttestationRecord(
                    assigned_name=gw_name, workspace_id=ws, role="codex",
                    lane_id=self.LANE, locator=attest_locator, verdict=VERDICT_PRESENT,
                    observed_at="2099-07-18T00:00:00+00:00", replacement_action_id="",
                )
            )
        return sa_id

    def test_real_detection_returns_exact_startup_id_and_fails_shut_on_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
            )
            request = PrepareBoundPairRequest(
                issue=self.ISSUE, journal="80925", lane=self.LANE,
                worktree=str(worktree), branch="main",
            )
            ops = LiveBoundPairPreparationOps(repo_root=coord, env=env)

            def _detect(action):
                with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                     mock.patch.object(PRP, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)):
                    return ops.rollback_owed_startup_action(request, action_id=action)

            # Happy path: the gateway is live at gw_old (a fresh slot vs the synthetic prior
            # locator), normal-v1 attested by the append, and its startup txn is rollback_owed.
            sa_id = self._seed_partial(home, ws, gw_name, gw_old, action="act-happy")
            self.assertEqual(_detect("act-happy"), sa_id)

            # Mismatch matrix -> "" (never this owned partial; left to the generic block).
            self._seed_partial(home, ws, gw_name, gw_old, action="act-no-receipt", receipt="")
            self.assertEqual(_detect("act-no-receipt"), "")
            self._seed_partial(home, ws, gw_name, gw_old, action="act-not-owed",
                               phase=PHASE_COMPLETED_SUCCESS)
            self.assertEqual(_detect("act-not-owed"), "")
            self._seed_partial(home, ws, gw_name, gw_old, action="act-stale-locator",
                               old_locator=gw_old)  # live == old -> not a fresh launch
            self.assertEqual(_detect("act-stale-locator"), "")
            self._seed_partial(home, ws, gw_name, gw_old, action="act-foreign-attest",
                               attest_locator="w9:pGHOST")  # attestation mismatches live
            self.assertEqual(_detect("act-foreign-attest"), "")
            # No side binding at all for this action.
            self.assertEqual(_detect("act-never-reserved"), "")

    def test_public_preflight_surfaces_rollback_command_with_startup_action_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
            )
            request = PrepareBoundPairRequest(
                issue=self.ISSUE, journal="80925", lane=self.LANE,
                worktree=str(worktree), branch="main",
            )
            # A self-consistent distinct-owner marker; its action id is the prep/replacement
            # action that keys the side binding (they are one id, #13933).
            expectation = expectation_for(
                issue=self.ISSUE, lane=self.LANE, revision=4, generation=1,
                resolved_worktree=str(worktree), worktree_identity="wt_preflight",
                branch="main",
                slots=(
                    BoundSlot(role="gateway", provider="codex", assigned_name=gw_name,
                              locator=gw_old, disposition=SLOT_HEALTHY),
                    BoundSlot(role="worker", provider="claude", assigned_name=wk_name,
                              locator=wk_old, disposition=SLOT_RECOVER),
                ),
                discard_roles=("worker",),
            )
            sa_id = self._seed_partial(home, ws, gw_name, gw_old, action=expectation.action_id)

            # A blocked observation (no discardable composer) so the preflight consults the
            # resume path; the marker + REAL rollback detection do the rest.
            blocked_obs = PreparationObservation(
                workspace_id=ws, worktree_path=str(worktree), worktree_identity="wt_preflight",
                branch="main", revision=4, generation=1, lifecycle_exact=True,
                pins_empty=True, pins_known=True, inventory_readable=True,
                worktree_readable=True, worktree_clean=True, branch_matches=True,
                slots=(
                    BoundSlot(role="gateway", provider="codex", assigned_name=gw_name,
                              locator=gw_old, disposition=SLOT_HEALTHY),
                    BoundSlot(role="worker", provider="claude", assigned_name=wk_name,
                              locator=wk_old, disposition=SLOT_HEALTHY),
                ),
                discard_roles=(),
            )

            class _SeamedPreflightOps(LiveBoundPairPreparationOps):
                # observe (lifecycle projection) and approval_fields (Redmine journal) are the
                # external inputs the test supplies deterministically; rollback_owed_startup_action
                # stays the REAL cross-store detection under test.
                def observe(self_inner, req, *, action_id=""):
                    return blocked_obs

                def approval_fields(self_inner, issue, journal):
                    return (expectation.marker_fields(),)

            ops = _SeamedPreflightOps(repo_root=coord, env=env)
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 mock.patch.object(PRP, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)):
                outcome = run_bound_pair_preparation(request, execute=False, ops=ops)

            self.assertEqual(outcome.state, STATE_BLOCKED)
            self.assertEqual(outcome.resume_diagnostic, RESUME_STARTUP_ROLLBACK_REQUIRED)
            self.assertEqual(outcome.startup_rollback_action_id, sa_id)
            self.assertFalse(outcome.executed)
            # The operator gets a ready-to-run public rollback command carrying the exact id.
            self.assertIn(f"--action-id {sa_id}", outcome.detail)
            self.assertIn("session-rollback", outcome.detail)
            # Value-safe: no locator / receipt bytes leak into the public surface.
            for secret in (gw_old, wk_old, "workspace=w1"):
                self.assertNotIn(secret, outcome.detail)
            self.assertEqual(outcome.as_payload()["startup_rollback_action_id"], sa_id)

    def test_without_a_rollback_owed_partial_the_old_no_progress_stands(self):
        # Negative control: identical seam, but NO seeded a14 partial. The preflight must fall
        # back to the prior `no_action_owned_progress` diagnostic (the surface is specific to
        # the real owned partial, never a blanket relabel of the resume path).
        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
            )
            request = PrepareBoundPairRequest(
                issue=self.ISSUE, journal="80925", lane=self.LANE,
                worktree=str(worktree), branch="main",
            )
            expectation = expectation_for(
                issue=self.ISSUE, lane=self.LANE, revision=4, generation=1,
                resolved_worktree=str(worktree), worktree_identity="wt_preflight",
                branch="main",
                slots=(
                    BoundSlot(role="gateway", provider="codex", assigned_name=gw_name,
                              locator=gw_old, disposition=SLOT_HEALTHY),
                    BoundSlot(role="worker", provider="claude", assigned_name=wk_name,
                              locator=wk_old, disposition=SLOT_RECOVER),
                ),
                discard_roles=("worker",),
            )
            blocked_obs = PreparationObservation(
                workspace_id=ws, worktree_path=str(worktree), worktree_identity="wt_preflight",
                branch="main", revision=4, generation=1, lifecycle_exact=True,
                pins_empty=True, pins_known=True, inventory_readable=True,
                worktree_readable=True, worktree_clean=True, branch_matches=True,
                slots=(
                    BoundSlot(role="gateway", provider="codex", assigned_name=gw_name,
                              locator=gw_old, disposition=SLOT_HEALTHY),
                    BoundSlot(role="worker", provider="claude", assigned_name=wk_name,
                              locator=wk_old, disposition=SLOT_HEALTHY),
                ),
                discard_roles=(),
            )

            class _SeamedPreflightOps(LiveBoundPairPreparationOps):
                def observe(self_inner, req, *, action_id=""):
                    return blocked_obs

                def approval_fields(self_inner, issue, journal):
                    return (expectation.marker_fields(),)

            ops = _SeamedPreflightOps(repo_root=coord, env=env)
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 mock.patch.object(PRP, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)):
                outcome = run_bound_pair_preparation(request, execute=False, ops=ops)
            self.assertNotEqual(outcome.resume_diagnostic, RESUME_STARTUP_ROLLBACK_REQUIRED)
            self.assertEqual(outcome.startup_rollback_action_id, "")

    def test_full_chain_preflight_id_then_public_rollback_then_replay_binds(self):
        # The threaded recovery chain (review j#82079 #2): seed the actual a14 partial -> the
        # read-only detection hands out the exact startup id -> the public rollback rail's
        # durable transition (mark_closed + completed_rolled_back, herdr_session_rollback.py
        # lines 520/546) closes the fresh slot -> replaying the SAME prepare/replacement action
        # relaunches and binds. No blind retry, no raw rollback, no old-slot re-close.
        from mozyo_bridge.core.state.startup_transaction_fence import (
            StartupTransactionFence as _Fence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
            )
            request = PrepareBoundPairRequest(
                issue=self.ISSUE, journal="80925", lane=self.LANE,
                worktree=str(worktree), branch="main",
            )
            action = "prep-full-chain-act"
            old_locator = "w1:pPREV"
            sa_id = self._seed_partial(
                home, ws, gw_name, gw_old, action=action, old_locator=old_locator
            )

            ops = LiveBoundPairPreparationOps(repo_root=coord, env=env)
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 mock.patch.object(PRP, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)):
                # 1. Read-only preflight detection hands out the exact public rollback id.
                self.assertEqual(
                    ops.rollback_owed_startup_action(request, action_id=action), sa_id
                )

                # 2. The public rollback rail's durable effect: the fresh slot is closed and
                #    the startup transaction is terminal-rolled-back.
                for pane, agent in list(fake._agents.items()):
                    if agent.name == gw_name:
                        del fake._agents[pane]
                fence = _Fence(home=home)
                fence.mark_closed(sa_id, "codex")
                fence.set_phase(sa_id, PHASE_COMPLETED_ROLLED_BACK)

                # 3. Replay the SAME action -> fresh relaunch + bind (never re-close gw_old).
                HerdrSublaneActuatorOps(
                    repo_root=coord, lane_label=self.LANE, issue=self.ISSUE, journal="80925",
                    env=env, runner=fake.run, replacement_action_id=action,
                    replacement_assigned_name=gw_name, replacement_old_locator=old_locator,
                ).heal_lane_column(str(worktree), target_provider="codex")

            gw_live = fake.agent_named(gw_name)
            self.assertIsNotNone(gw_live)
            self.assertNotIn(gw_live["pane_id"], {gw_old, old_locator})  # fresh generation
            record = HerdrIdentityAttestationStore(home=home).read(gw_name)
            self.assertTrue(
                replacement_action_is_bound(
                    record, action_id=action, live_locator=gw_live["pane_id"],
                    expected_workspace_id=ws, expected_role="codex",
                    expected_lane=self.LANE, expected_assigned_name=gw_name,
                    expected_old_locator=old_locator, home=home,
                )
            )
            # The replay reached a NEW durable startup success, and the same id is no longer
            # rollback-owed, so a second preflight no longer offers a rollback.
            with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                 mock.patch.object(PRP, "list_herdr_agent_rows", lambda e: _agent_list_rows(fake)):
                self.assertEqual(
                    ops.rollback_owed_startup_action(request, action_id=action), ""
                )


if __name__ == "__main__":
    unittest.main()
