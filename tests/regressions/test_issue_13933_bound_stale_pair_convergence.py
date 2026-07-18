"""Regression matrix for #13933's bounded hibernated pair convergence rail."""

from __future__ import annotations

import os
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
)
from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer, ProcessGenerationPin
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
    sublane_quarantine as QM,
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


def _fake_binary(tmp: str) -> Path:
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


def _agent_list_rows(fake: FakeHerdr):
    import json

    out = fake.run(["herdr", "agent", "list"]).stdout
    return json.loads(out).get("agents", [])


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


if __name__ == "__main__":
    unittest.main()
