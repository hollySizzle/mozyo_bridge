"""Regression contract for #13933's separate pending-composer preparation rail."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.core.state.replacement_preservation import (
    PreservationObservation,
    assess_preservation,
)
from mozyo_bridge.core.state.replacement_transaction import (
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    sublane_quarantine as quarantine_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (
    ReplacementActuatorUseCase,
)
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
    BLOCK_NOT_BOUND_SIGNATURE,
    BLOCK_PAIR_PRESERVED,
    BLOCK_REPLACEMENT_STOPPED,
    RESUME_ADOPTED,
    RESUME_APPROVAL_UNREADABLE,
    RESUME_NO_OWNED_PROGRESS,
    RESUME_NO_OWNING_APPROVAL,
    RESUME_PROJECTED_BLOCKED,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (
    ACTUATION_EFFECT_FAILED,
    ACTUATION_IN_PROGRESS,
    ACTUATION_RECOVERED,
    ATTEST_BOUND,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
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


def _expectation(observation: PreparationObservation) -> PreparationExpectation:
    return expectation_for(
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


def _participant(role: str, *, phase: str = PARTICIPANT_CLOSE_OWED) -> ParticipantPin:
    slot = next(item for item in _observation().slots if item.role == role)
    return ParticipantPin(
        lane_id=REQ.lane,
        role=role,
        provider=slot.provider,
        assigned_name=slot.assigned_name,
        old_locator=slot.locator,
        lane_revision="4",
        lane_generation="1",
        phase=phase,
    )


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
        expectation = _expectation(_observation())
        port = _ComposerDiscardActuatorPort(
            owner, request, expectation, live, REQ, ("gateway",)
        )
        fresh = _observation(
            slots=(
                _slot("gateway", disposition),
                _slot("worker", SLOT_RECOVER),
            ),
            discard_roles=("gateway",) if composer_ok and disposition == SLOT_PRESERVE_PENDING else (),
        )
        port._fresh_authority = mock.Mock(
            return_value=(
                fresh
                if composer_ok and disposition == SLOT_PRESERVE_PENDING
                else None
            )
        )
        return port

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

    def _observe(self, port, *, authority=True):
        if not authority:
            port._fresh_authority.return_value = None
        return assess_preservation(port.observe_preservation(self._pin()))

    def test_exact_approved_uncorrelated_pending_slot_is_the_only_close_carveout(self):
        self.assertTrue(self._observe(self._port()).may_close)
        self.assertTrue(self._observe(self._port(composer_ok=False)).blocked)
        self.assertTrue(
            self._observe(self._port(disposition=SLOT_PRESERVE_PRODUCTIVE)).blocked
        )
        self.assertTrue(self._observe(self._port(), authority=False).blocked)

    def test_absent_close_owed_slot_without_transaction_close_proof_is_ambiguous(self):
        port = self._port()
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live.list_herdr_agent_rows"
        )
        with mock.patch(module, return_value=()):
            self.assertEqual(port.observe_old_slot(self._pin()), OLD_SLOT_AMBIGUOUS)


class FullPairRetryAuthorityTests(unittest.TestCase):
    def test_nonparticipant_identity_locator_and_disposition_drift_are_rejected(self):
        approved = _observation()
        expectation = _expectation(approved)
        participant = _participant("gateway")
        worker = _slot("worker", SLOT_RECOVER)
        mutations = (
            BoundSlot("worker", "foreign", worker.assigned_name, worker.locator, SLOT_RECOVER),
            BoundSlot("worker", worker.provider, "foreign-worker", worker.locator, SLOT_RECOVER),
            BoundSlot("worker", worker.provider, worker.assigned_name, "w9:p9", SLOT_RECOVER),
            BoundSlot(
                "worker", worker.provider, worker.assigned_name, worker.locator,
                SLOT_PRESERVE_PRODUCTIVE,
            ),
        )
        for changed in mutations:
            with self.subTest(changed=changed):
                current = _observation(
                    slots=(_slot("gateway", SLOT_PRESERVE_PENDING), changed)
                )
                self.assertFalse(
                    LiveBoundPairPreparationOps._progress_snapshot_matches(
                        REQ, current, expectation, (participant,)
                    )
                )

    def test_existing_close_owed_retry_blocks_before_actuator_on_other_role_drift(self):
        approved = _observation()
        expectation = _expectation(approved)
        current = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                BoundSlot("worker", "claude", "managed-worker", "w9:p9", SLOT_RECOVER),
            )
        )
        existing = SimpleNamespace(participants=(_participant("gateway"),))
        store = SimpleNamespace(get=mock.Mock(return_value=existing))
        ops = LiveBoundPairPreparationOps(
            repo_root=Path("/coordinator"), env={}, transaction_store=store
        )
        ops.observe = mock.Mock(return_value=current)
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_composer_discard_live."
            "ReplacementActuatorUseCase"
        )
        with mock.patch(module) as actuator:
            result = ops.drive(REQ, expectation, approved)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "transaction_conflict")
        actuator.assert_not_called()

    def test_close_and_launch_edges_reject_a_late_full_pair_race_without_effect(self):
        port = CloseBoundaryTests()._port()
        port._fresh_authority = _ComposerDiscardActuatorPort._fresh_authority.__get__(
            port, _ComposerDiscardActuatorPort
        )
        changed = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                BoundSlot("worker", "claude", "managed-worker", "w9:p9", SLOT_RECOVER),
            )
        )
        transaction = SimpleNamespace(participants=(_participant("gateway"),))
        port.owner.transaction_store = SimpleNamespace(
            get=mock.Mock(return_value=transaction)
        )
        port.owner._observation_from_snapshot = mock.Mock(return_value=changed)
        port.owner._progress_proven_roles = mock.Mock(return_value=())
        base = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live._BoundPairActuatorPort"
        )
        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_composer_discard_live"
        )

        def git_result(_worktree, *args):
            return (True, REQ.branch) if args == ("branch", "--show-current") else (True, "")

        with mock.patch(f"{module}.list_herdr_agent_rows", return_value=()), mock.patch(
            f"{module}._git", side_effect=git_result
        ), mock.patch(f"{base}.close_exact_generation") as close, mock.patch(
            f"{base}.launch_action_bound"
        ) as launch, mock.patch(
            f"{base}.verify_attestation", return_value=ATTEST_BOUND
        ) as verify:
            pin = CloseBoundaryTests._pin()
            self.assertEqual(port.close_exact_generation(pin), CLOSE_ERROR)
            self.assertEqual(port.launch_action_bound("action", pin), LAUNCH_ERROR)
            self.assertEqual(port.verify_attestation("action", pin), ATTEST_PENDING)
        close.assert_not_called()
        launch.assert_not_called()
        verify.assert_called_once()

    def test_proven_launch_owed_partial_retry_preserves_approved_projection(self):
        approved = _observation()
        expectation = _expectation(approved)
        participant = _participant("gateway", phase=PARTICIPANT_LAUNCH_OWED)
        current = _observation(
            slots=(
                BoundSlot(
                    "gateway", participant.provider, participant.assigned_name,
                    participant.old_locator, SLOT_RECOVER, close_proven=True,
                ),
                _slot("worker", SLOT_RECOVER),
            ),
            discard_roles=(),
        )
        self.assertTrue(
            LiveBoundPairPreparationOps._progress_snapshot_matches(
                REQ,
                current,
                expectation,
                (participant,),
                progress_proven_roles=("gateway",),
            )
        )
        verifying = _participant("gateway", phase=PARTICIPANT_VERIFY_OWED)
        launched = _observation(
            slots=(
                BoundSlot(
                    "gateway", verifying.provider, verifying.assigned_name,
                    "w1:p9", SLOT_RECOVER,
                ),
                _slot("worker", SLOT_RECOVER),
            ),
            discard_roles=(),
        )
        ops = LiveBoundPairPreparationOps(repo_root=Path("/coordinator"), env={})
        proven = ops._progress_proven_roles(
            REQ, launched, expectation, (verifying,)
        )
        self.assertEqual(proven, ("gateway",))
        self.assertTrue(
            ops._progress_snapshot_matches(
                REQ,
                launched,
                expectation,
                (verifying,),
                progress_proven_roles=proven,
            )
        )
        store_path = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_composer_discard_live."
            "HerdrIdentityAttestationStore.read"
        )
        evaluate_path = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_composer_discard_live."
            "evaluate_attestation"
        )
        attestation = SimpleNamespace(replacement_action_id=expectation.action_id)
        with mock.patch(store_path, return_value=attestation), mock.patch(
            evaluate_path,
            side_effect=(SimpleNamespace(ok=False), SimpleNamespace(ok=True)),
        ):
            self.assertFalse(
                ops._action_bound_slot(
                    REQ,
                    launched,
                    expectation,
                    verifying,
                    require_attestation=True,
                )
            )
            self.assertTrue(
                ops._action_bound_slot(
                    REQ,
                    launched,
                    expectation,
                    verifying,
                    require_attestation=True,
                )
            )

    def test_two_role_sequence_uses_immutable_first_progress_and_exact_second_close(self):
        approved = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("gateway", "worker"),
        )
        expectation = _expectation(approved)
        gateway = _participant("gateway", phase=PARTICIPANT_REPLACED)
        worker = _participant("worker")
        current = _observation(
            slots=(
                BoundSlot(
                    "gateway", gateway.provider, gateway.assigned_name,
                    "w1:p9", SLOT_HEALTHY,
                ),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("worker",),
        )
        self.assertTrue(
            LiveBoundPairPreparationOps._progress_snapshot_matches(
                REQ,
                current,
                expectation,
                (gateway, worker),
                progress_proven_roles=("gateway",),
            )
        )


class PartialEffectPairCoherenceTests(unittest.TestCase):
    """#13846 j#80933 live shape: this action closed the gateway, then the launch failed.

    The pair fence inherited from the quarantine classifier requires BOTH provider rows live,
    so the action's own close made the role still owed read as ``generation_mismatch`` — the
    transaction could never be replayed (j#80934).  These drive the real classifier: the
    previous rails only ever asserted against hand-authored ``discard_roles``.
    """

    WS = "mzb1workspace"
    GATEWAY_PROVIDER = "codex"
    WORKER_PROVIDER = "claude"

    def setUp(self) -> None:
        self.worktree = tempfile.mkdtemp()
        self.request = PrepareBoundPairRequest(
            issue="13933", journal="80925", lane=REQ.lane,
            worktree=self.worktree, branch=REQ.branch,
        )
        self.gateway_name = encode_assigned_name(self.WS, self.GATEWAY_PROVIDER, REQ.lane)
        self.worker_name = encode_assigned_name(self.WS, self.WORKER_PROVIDER, REQ.lane)

    def _row(self, name, locator, *, tab="w28:t1"):
        return {
            "name": name, "pane_id": locator, "workspace_id": self.WS,
            "tab_id": tab, "revision": 7, "foreground_cwd": self.worktree,
        }

    def _discardable(self, rows, *, action_closed=()):
        ops = LiveBoundPairPreparationOps(repo_root=Path(self.worktree), env={})
        with mock.patch.object(
            quarantine_module, "repo_scope_workspace_id", return_value=self.WS
        ), mock.patch.object(
            quarantine_module, "resolve_gateway_provider", return_value=self.GATEWAY_PROVIDER
        ), mock.patch.object(
            quarantine_module, "resolve_worker_provider", return_value=self.WORKER_PROVIDER
        ), mock.patch.object(
            quarantine_module, "_resolve_binary_or_die", return_value="herdr"
        ), mock.patch.object(
            quarantine_module, "HerdrCliAgentStateReader"
        ) as state, mock.patch.object(
            quarantine_module, "HerdrCliTransport"
        ) as transport, mock.patch.object(
            quarantine_module, "observe_composer_text",
            return_value=quarantine_module.ComposerObservation(True, True),
        ):
            state.return_value.read_agent_state.return_value = SimpleNamespace(
                ok=True, state="idle"
            )
            transport.return_value.read_pane.return_value = SimpleNamespace(
                ok=True, content="unsent composer text"
            )
            return ops._composer_discardable(
                self.request, role="worker", provider=self.WORKER_PROVIDER,
                assigned_name=self.worker_name, locator="w28:p5J", rows=rows,
                action_closed_roles=action_closed,
            )

    def _both(self):
        return [
            self._row(self.gateway_name, "w28:p5H"),
            self._row(self.worker_name, "w28:p5J"),
        ]

    def _worker_only(self):
        return [self._row(self.worker_name, "w28:p5J")]

    def test_intact_pair_keeps_its_pending_worker_discardable(self):
        self.assertTrue(self._discardable(self._both()))

    def test_sibling_this_action_closed_keeps_the_owed_role_discardable(self):
        # The exact j#80934 deadlock: without the proof the action's own effect disqualifies
        # the work it still owes, so the immutable transaction can never be replayed.
        self.assertFalse(self._discardable(self._worker_only()))
        self.assertTrue(
            self._discardable(
                self._worker_only(), action_closed=(self.GATEWAY_PROVIDER,)
            )
        )

    def test_reappeared_sibling_falls_back_to_the_inherited_live_pair_fence(self):
        # Claiming the gateway is action-closed must not launder a live row: a sibling that
        # came back is judged by the inherited fence, which rejects a split placement.
        split = [
            self._row(self.gateway_name, "w28:p5H", tab="w28:t9"),
            self._row(self.worker_name, "w28:p5J"),
        ]
        self.assertFalse(
            self._discardable(split, action_closed=(self.GATEWAY_PROVIDER,))
        )

    def test_foreign_absence_is_never_admitted_as_this_actions_progress(self):
        # The gateway is gone, but nothing proves THIS action closed it.
        self.assertFalse(self._discardable(self._worker_only(), action_closed=()))

    def test_wholly_action_closed_pair_has_no_composer_left_to_discard(self):
        self.assertFalse(
            self._discardable(
                self._worker_only(),
                action_closed=(self.GATEWAY_PROVIDER, self.WORKER_PROVIDER),
            )
        )


class PartialProgressPreflightTests(unittest.TestCase):
    """A half-replaced pair must report the replay it owns, not an unactionable block."""

    def _ops_at_partial_progress(self):
        approved = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("gateway", "worker"),
        )
        expectation = _expectation(approved)
        gateway = _participant("gateway", phase=PARTICIPANT_LAUNCH_OWED)
        # Raw: the gateway this action closed has no live row, so no role is discardable.
        raw = _observation(
            slots=(
                BoundSlot("gateway", gateway.provider, gateway.assigned_name, "", SLOT_RECOVER),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=(),
        )
        # Projected under the approved action id: the close this transaction proves.
        projected = _observation(
            slots=(
                BoundSlot(
                    "gateway", gateway.provider, gateway.assigned_name,
                    gateway.old_locator, SLOT_RECOVER, close_proven=True,
                ),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("worker",),
        )

        class _Ops(FakeOps):
            def observe(self, request, *, action_id=""):
                self.calls.append(("observe", action_id))
                return projected if action_id == expectation.action_id else raw

        ops = _Ops(raw)
        ops.markers = (expectation.marker_fields(),)
        return ops, expectation

    def test_partial_progress_preflight_reports_the_owned_replay(self):
        ops, expectation = self._ops_at_partial_progress()
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)

        self.assertEqual(outcome.state, STATE_ACTIONABLE)
        self.assertTrue(outcome.resuming)
        self.assertEqual(outcome.action_id, expectation.action_id)
        # The replay stays bound to the approval already recorded; no new marker is minted.
        self.assertEqual(outcome.approval_marker, expectation.marker())
        self.assertFalse(outcome.executed)
        self.assertNotIn("drive", [call[0] for call in ops.calls])

    def test_preflight_without_an_owning_action_keeps_the_original_block(self):
        ops, _expectation = self._ops_at_partial_progress()
        # No approval recorded: nothing anchors a replay, so the block must stand.
        ops.markers = ()
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)

        self.assertEqual(outcome.reason, BLOCK_NO_DISCARDABLE_COMPOSER)
        self.assertFalse(outcome.resuming)

    def test_approval_that_projects_nothing_is_not_a_replay(self):
        # The approval resolves, but the action owns no progress on this pair: the observation
        # is unchanged under its id, so this is a genuine block, not a resume.
        blocked = _observation(
            slots=(_slot("gateway", SLOT_HEALTHY), _slot("worker", SLOT_RECOVER)),
            discard_roles=(),
        )
        ops = FakeOps(blocked)
        ops.markers = (_expectation(_observation()).marker_fields(),)
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)

        self.assertEqual(outcome.reason, BLOCK_NO_DISCARDABLE_COMPOSER)
        self.assertFalse(outcome.resuming)

    def test_unreadable_approval_source_resumes_nothing(self):
        ops, _expectation = self._ops_at_partial_progress()

        def _raise(issue, journal):
            raise RuntimeError("redmine credential missing")

        ops.approval_fields = _raise
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)

        self.assertEqual(outcome.reason, BLOCK_NO_DISCARDABLE_COMPOSER)
        self.assertFalse(outcome.resuming)


class ResumeDiagnosticAndPublicStatusTests(unittest.TestCase):
    """Every declined resume names WHY, and a launch failure is typed in the public payload.

    Redmine #13933 R7 (design answer j#81046 Decision 2/3, review finding F2 j#81182). The four
    silent ``return None`` paths made an unreadable credential and a pair with no owning action
    produce byte-identical output; each now carries a typed ``resume_diagnostic``.
    """

    def _blocking_initial(self) -> PreparationObservation:
        # A pair that is otherwise bound but has no discardable composer: the terminal block
        # that invites the resume preflight to look for an owning action.
        return _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=(),
        )

    def _partial_progress_ops(self):
        approved = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("gateway", "worker"),
        )
        expectation = _expectation(approved)
        gateway = _participant("gateway", phase=PARTICIPANT_LAUNCH_OWED)
        raw = _observation(
            slots=(
                BoundSlot("gateway", gateway.provider, gateway.assigned_name, "", SLOT_RECOVER),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=(),
        )
        projected = _observation(
            slots=(
                BoundSlot(
                    "gateway", gateway.provider, gateway.assigned_name,
                    gateway.old_locator, SLOT_RECOVER, close_proven=True,
                ),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("worker",),
        )

        class _Ops(FakeOps):
            def observe(self, request, *, action_id=""):
                self.calls.append(("observe", action_id))
                return projected if action_id == expectation.action_id else raw

        ops = _Ops(raw)
        ops.markers = (expectation.marker_fields(),)
        return ops, expectation

    def test_adopted_diagnostic_on_owned_replay(self):
        ops, _expectation = self._partial_progress_ops()
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertTrue(outcome.resuming)
        self.assertEqual(outcome.resume_diagnostic, RESUME_ADOPTED)
        self.assertEqual(outcome.as_payload()["resume_diagnostic"], RESUME_ADOPTED)

    def test_no_matching_approval_marker_diagnostic(self):
        ops, _expectation = self._partial_progress_ops()
        ops.markers = ()
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertFalse(outcome.resuming)
        self.assertEqual(outcome.resume_diagnostic, RESUME_NO_OWNING_APPROVAL)

    def test_no_action_owned_progress_diagnostic(self):
        # The approval resolves, but re-observing under its id changes nothing.
        blocked = self._blocking_initial()
        ops = FakeOps(blocked)
        ops.markers = (_expectation(_observation()).marker_fields(),)
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertFalse(outcome.resuming)
        self.assertEqual(outcome.resume_diagnostic, RESUME_NO_OWNED_PROGRESS)

    def test_projected_still_blocked_diagnostic_carries_the_projected_reason(self):
        # The action owns progress (projection differs), but the projected pair is still blocked
        # on its own merits -- a preserved slot the approval does not cover.
        raw = self._blocking_initial()
        projected = _observation(
            slots=(
                _slot("gateway", SLOT_PRESERVE_PENDING),
                _slot("worker", SLOT_PRESERVE_PENDING),
            ),
            discard_roles=("gateway",),  # worker still preserved, not approved -> PAIR_PRESERVED
        )

        class _Ops(FakeOps):
            def observe(self, request, *, action_id=""):
                self.calls.append(("observe", action_id))
                return projected if action_id else raw

        ops = _Ops(raw)
        ops.markers = (_expectation(_observation()).marker_fields(),)
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertFalse(outcome.resuming)
        self.assertTrue(outcome.resume_diagnostic.startswith(RESUME_PROJECTED_BLOCKED + ":"))
        self.assertIn(BLOCK_PAIR_PRESERVED, outcome.resume_diagnostic)

    def test_unreadable_approval_source_reports_type_only_no_message_leak(self):
        ops, _expectation = self._partial_progress_ops()

        def _raise(issue, journal):
            raise RuntimeError("SECRET-CREDENTIAL-abc123")

        ops.approval_fields = _raise
        outcome = run_bound_pair_preparation(REQ, execute=False, ops=ops)
        self.assertFalse(outcome.resuming)
        self.assertEqual(
            outcome.resume_diagnostic, f"{RESUME_APPROVAL_UNREADABLE}:RuntimeError"
        )
        # The exception's MESSAGE (which may quote credential/journal content) never escapes.
        self.assertNotIn("SECRET-CREDENTIAL-abc123", outcome.resume_diagnostic)
        self.assertNotIn("SECRET", str(outcome.as_payload()))

    def test_effect_failed_is_typed_in_the_public_replacement_status(self):
        # Decision 3: a launch failure at --execute is observable in the public JSON, not a
        # bare block.  ``replacement_status`` carries the actuator's typed stop reason.
        ops = FakeOps()
        _preflight, _fields = _authorize(ops)
        ops.drive_result = PreparationDrive(False, "effect_failed")
        outcome = run_bound_pair_preparation(REQ, execute=True, ops=ops)
        self.assertTrue(outcome.is_blocked)
        self.assertEqual(outcome.reason, BLOCK_REPLACEMENT_STOPPED)
        self.assertEqual(outcome.replacement_status, "effect_failed")
        self.assertEqual(outcome.as_payload()["replacement_status"], "effect_failed")

    def test_resume_execute_replays_the_same_action_without_a_new_marker(self):
        # Decision 3: the resume reaches the drive under the approval already recorded -- the
        # same immutable transaction id, no fresh marker minted.  Re-closing the already-closed
        # role is prevented by the close-proof classifier (PartialEffectPairCoherenceTests).
        ops, expectation = self._partial_progress_ops()
        ops.drive_result = PreparationDrive(True, ACTUATION_RECOVERED)
        outcome = run_bound_pair_preparation(REQ, execute=True, ops=ops)
        drive_calls = [c for c in ops.calls if c[0] == "drive"]
        self.assertEqual(drive_calls, [("drive", expectation.action_id)])
        self.assertEqual(outcome.action_id, expectation.action_id)


class _CountingActuatorPort:
    """A synthetic ExactGenerationActuatorPort that counts close/launch/verify per identity.

    No live process, no DB.  ``launch_result`` injects a launch failure for a scripted first
    run; ``closed`` / ``launched`` / ``verified`` are the tripwires that prove a replay never
    re-closes an already-closed identity (Redmine #13933 R7 F2-remain / j#81046 Decision 3).
    """

    def __init__(self) -> None:
        self.old: dict[tuple, str] = {}
        self.attest: dict[tuple, str] = {}
        self.launch_result: dict[tuple, str] = {}
        self.closed: list[tuple] = []
        self.launched: list[tuple] = []
        self.verified: list[tuple] = []
        self._pres = PreservationObservation(identity_matches=True, attestation_fresh=True)

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return self.old.get(pin.identity, OLD_SLOT_PRESENT)

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        return self._pres

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        self.closed.append(pin.identity)
        return CLOSE_DONE

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        self.launched.append(pin.identity)
        return self.launch_result.get(pin.identity, LAUNCH_DONE)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        self.verified.append(pin.identity)
        return self.attest.get(pin.identity, ATTEST_BOUND)

    def _fresh_authority(self, *, require_attested_roles=()):
        # ``_finish`` re-reads full pair authority at each completion edge; a live pair is
        # non-None.  ``None`` would leave the transaction retryably draining.
        return self


class PartialReplayEndToEndTests(unittest.TestCase):
    """The launch-failure replay drives the REAL actuator + REAL transaction store.

    Redmine #13933 R7 F2-remain (review j#81211): the FakeOps drive test asserted only that
    ``drive()`` was called once with the same action id.  This drives the actuator seam
    ``prepare-bound-pair --execute`` delegates to -- ``ReplacementActuatorUseCase
    .drive_worker_recovery`` over a real ``ReplacementTransactionStore`` -- so an execute whose
    launch fails, then replays under the SAME immutable action, is shown to (1) surface the
    typed ``effect_failed`` status the public ``replacement_status`` carries verbatim, (2) never
    re-close an already-closed identity, and (3) converge every participant to ``replaced``.
    """

    GEN = 1
    # A FUTURE clock so the lease the actuator mints stays live when `_finish` re-reads it
    # under the store's real clock (mirrors VerifyAndCompletionAuthorityTests).
    FIXED = "2099-07-17T12:00:00+00:00"

    def _participants(self):
        # The #13933 discard shape: the exact uncorrelated pending composer roles of one pair.
        return [
            ParticipantPin(
                lane_id=REQ.lane, role="gateway", provider="codex",
                assigned_name="mzb1-gw", old_locator="w28:p3G",
                lane_revision="4", lane_generation="1",
            ),
            ParticipantPin(
                lane_id=REQ.lane, role="worker", provider="claude",
                assigned_name="mzb1-wk", old_locator="w28:p3H",
                lane_revision="4", lane_generation="1",
            ),
        ]

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("ws", "prepare-bound-pair-abc")
        self.participants = self._participants()
        self.store.plan_transaction(
            self.key,
            action_generation=self.GEN,
            decision=DecisionPointer(source="redmine", issue_id="13846", journal_id="80925"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13846", journal_id="80925",
                expected_gate="bound_pair_composer_discard_approval",
                next_semantic_action="converge_bound_pair",
            ),
            participants=self.participants,
        )
        self.port = _CountingActuatorPort()
        # The public `.drive()` runs the actuator over THIS store, then completes the
        # transaction through `_finish` under the same holder/action -- so the completion leg
        # is driven over the same real store, not a separate use case.
        self.ops = LiveBoundPairPreparationOps(
            repo_root=self.home, transaction_store=self.store
        )
        self.expectation = PreparationExpectation(
            issue="13846", lane=REQ.lane, revision=4, generation=1,
            action_generation=self.GEN, action_id="prepare-bound-pair-abc",
            worktree_digest="wt", slot_digest="sd",
            discard_roles=("gateway", "worker"),
        )

    def _drive(self):
        return ReplacementActuatorUseCase(
            self.store, self.port, preservation_policy=assess_preservation,
            clock=lambda: self.FIXED,
        ).drive_worker_recovery(
            self.key, holder="H", expected_action_generation=self.GEN
        )

    def _finish(self) -> bool:
        return self.ops._finish(self.key, self.expectation, "H", self.port)

    def _phase(self, pin: ParticipantPin) -> str:
        return self.store.get(self.key).find_participant(pin.identity).phase

    def test_launch_failure_then_same_action_replay_never_re_closes_and_converges(self):
        gateway, worker = self.participants
        # Run 1: the gateway's launch fails after its close commits -- the exact j#80933 shape.
        self.port.launch_result[gateway.identity] = LAUNCH_ERROR
        first = self._drive()

        # (1) the typed status the public `replacement_status` surfaces verbatim (see
        # LiveBoundPairPreparationOps.drive: `PreparationDrive(False, result.status)`).
        self.assertEqual(first.status, ACTUATION_EFFECT_FAILED)
        self.assertEqual(ACTUATION_EFFECT_FAILED, "effect_failed")
        self.assertNotEqual(first.status, ACTUATION_RECOVERED)
        # the close committed; the gateway is now owed a launch, not another close.
        self.assertEqual(self.port.closed.count(gateway.identity), 1)
        self.assertEqual(self._phase(gateway), PARTICIPANT_LAUNCH_OWED)

        # Run 2: the SAME immutable action + generation; the launch now succeeds.
        self.port.launch_result.pop(gateway.identity)
        second = self._drive()

        self.assertEqual(second.status, ACTUATION_RECOVERED)
        # (2) the already-closed gateway is NEVER re-closed on the replay.
        self.assertEqual(self.port.closed.count(gateway.identity), 1)
        # (3) every participant converges to replaced; the still-owed worker is processed and
        #     each identity is closed exactly once and verified after an action-bound launch.
        for pin in self.participants:
            self.assertEqual(self._phase(pin), PARTICIPANT_REPLACED, pin.role)
            self.assertEqual(self.port.closed.count(pin.identity), 1, pin.role)
            self.assertIn(pin.identity, self.port.launched)
            self.assertIn(pin.identity, self.port.verified)
        # The actuator stops at replacing_nonself holding the lease; `.drive` then runs the
        # completion leg (`_finish`) over the SAME store/holder/action.  Drive it here so this
        # one replay fixture proves the top-level record reaches PHASE_COMPLETED (R8 j#81211
        # / R9 j#81222 required correction), not merely that participants are replaced.
        self.assertEqual(self.store.get(self.key).phase, PHASE_REPLACING_NONSELF)
        self.assertTrue(self._finish())
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)
        # completion never re-closed anything.
        self.assertEqual(self.port.closed.count(gateway.identity), 1)

    def test_clean_run_recovers_and_completes_the_transaction(self):
        result = self._drive()
        self.assertEqual(result.status, ACTUATION_RECOVERED)
        for pin in self.participants:
            self.assertEqual(self._phase(pin), PARTICIPANT_REPLACED, pin.role)
            self.assertEqual(self.port.closed.count(pin.identity), 1, pin.role)
        self.assertTrue(self._finish())
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)

    def test_finish_is_idempotent_on_a_completed_transaction(self):
        self._drive()
        self.assertTrue(self._finish())
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)
        closed_after = list(self.port.closed)
        # A replay of the completion leg on an already-completed record writes nothing.
        self.assertTrue(self._finish())
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)
        self.assertEqual(self.port.closed, closed_after)


class VerifyAndCompletionAuthorityTests(unittest.TestCase):
    # Keep the synthetic lease live even when `_finish` uses the store's real clock.
    now = "2099-07-17T10:45:00+00:00"
    expiry = "2100-07-17T10:45:00+00:00"

    def _store_at_verify(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ReplacementTransactionStore(home=Path(temporary.name))
        expectation = _expectation(_observation())
        key = ReplacementTransactionKey("mzb1_workspace", expectation.action_id)
        holder = f"prepare:{expectation.action_id}:g{expectation.action_generation}"
        planned = store.plan_transaction(
            key,
            action_generation=expectation.action_generation,
            decision=DecisionPointer(
                source="redmine", issue_id=REQ.issue, journal_id=REQ.journal
            ),
            continuation=ContinuationPointer(
                source="redmine",
                issue_id=REQ.issue,
                journal_id=REQ.journal,
                expected_gate=APPROVAL_GATE,
                next_semantic_action="converge_bound_pair",
            ),
            participants=(_participant("gateway"),),
        )
        self.assertTrue(planned.applied)
        record = store.get(key)
        claimed = store.claim(
            key,
            expected_revision=record.revision,
            expected_action_generation=expectation.action_generation,
            holder=holder,
            lease_expires_at=self.expiry,
            now=self.now,
        )
        self.assertTrue(claimed.applied)
        for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
            record = store.get(key)
            moved = store.transition_phase(
                key,
                expected_revision=record.revision,
                expected_action_generation=expectation.action_generation,
                target=phase,
                holder=holder,
                now=self.now,
            )
            self.assertTrue(moved.applied)
        for phase in (PARTICIPANT_LAUNCH_OWED, PARTICIPANT_VERIFY_OWED):
            record = store.get(key)
            moved = store.transition_participant(
                key,
                expected_revision=record.revision,
                expected_action_generation=expectation.action_generation,
                identity=record.participants[0].identity,
                target=phase,
                holder=holder,
                now=self.now,
            )
            self.assertTrue(moved.applied)
        return store, key, expectation, holder

    @staticmethod
    def _phase(store, key):
        return store.get(key).participants[0].phase

    def test_verify_rechecks_full_pair_after_bound_attestation(self):
        port = CloseBoundaryTests()._port()
        port._fresh_authority.return_value = None
        base = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live."
            "_BoundPairActuatorPort.verify_attestation"
        )
        with mock.patch(base, return_value=ATTEST_BOUND) as inherited:
            self.assertEqual(
                port.verify_attestation("action", CloseBoundaryTests._pin()),
                ATTEST_PENDING,
            )
        inherited.assert_called_once()
        port._fresh_authority.assert_called_once_with(
            require_attested_roles=("gateway",)
        )

    def test_verify_authority_race_writes_zero_participant_phase(self):
        store, key, expectation, holder = self._store_at_verify()
        port = CloseBoundaryTests()._port()
        port.owner.transaction_store = store
        port._fresh_authority.return_value = None
        base = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live."
            "_BoundPairActuatorPort.verify_attestation"
        )
        with mock.patch(base, return_value=ATTEST_BOUND), mock.patch.object(
            store, "transition_participant", wraps=store.transition_participant
        ) as transition:
            result = ReplacementActuatorUseCase(
                store, port, clock=lambda: self.now
            ).drive_worker_recovery(
                key,
                holder=holder,
                expected_action_generation=expectation.action_generation,
            )
        self.assertEqual(result.status, ACTUATION_IN_PROGRESS)
        self.assertEqual(self._phase(store, key), PARTICIPANT_VERIFY_OWED)
        transition.assert_not_called()

    def test_finish_authority_race_before_first_leg_writes_zero_phase(self):
        expectation = _expectation(_observation())
        holder = "holder"
        record = SimpleNamespace(
            phase=PHASE_REPLACING_NONSELF, revision=7, lease_holder=holder
        )
        store = SimpleNamespace(
            get=mock.Mock(return_value=record),
            transition_phase=mock.Mock(),
            release=mock.Mock(),
        )
        ops = LiveBoundPairPreparationOps(
            repo_root=Path("/coordinator"), env={}, transaction_store=store
        )
        authority = SimpleNamespace(_fresh_authority=mock.Mock(return_value=None))
        self.assertFalse(
            ops._finish(
                ReplacementTransactionKey("mzb1_workspace", expectation.action_id),
                expectation,
                holder,
                authority,
            )
        )
        store.transition_phase.assert_not_called()

    def test_finish_authority_race_before_final_leg_writes_zero_completion(self):
        expectation = _expectation(_observation())
        holder = "holder"
        replacing = SimpleNamespace(
            phase=PHASE_REPLACING_NONSELF, revision=7, lease_holder=holder
        )
        draining = SimpleNamespace(
            phase=PHASE_DRAINING_CONTINUATION, revision=8, lease_holder=holder
        )
        store = SimpleNamespace(
            get=mock.Mock(side_effect=(replacing, draining)),
            transition_phase=mock.Mock(return_value=SimpleNamespace(applied=True)),
            release=mock.Mock(),
        )
        ops = LiveBoundPairPreparationOps(
            repo_root=Path("/coordinator"), env={}, transaction_store=store
        )
        authority = SimpleNamespace(
            _fresh_authority=mock.Mock(side_effect=(_observation(), None))
        )
        self.assertFalse(
            ops._finish(
                ReplacementTransactionKey("mzb1_workspace", expectation.action_id),
                expectation,
                holder,
                authority,
            )
        )
        self.assertEqual(store.transition_phase.call_count, 1)
        self.assertEqual(
            store.transition_phase.call_args.kwargs["target"],
            PHASE_DRAINING_CONTINUATION,
        )

    def test_valid_verify_and_replaced_retry_complete_under_fresh_authority(self):
        store, key, expectation, holder = self._store_at_verify()
        port = CloseBoundaryTests()._port()
        port.owner.transaction_store = store
        port._fresh_authority.return_value = _observation()
        base = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_hibernated_bound_pair_convergence_live."
            "_BoundPairActuatorPort.verify_attestation"
        )
        with mock.patch(base, return_value=ATTEST_BOUND):
            result = ReplacementActuatorUseCase(
                store, port, clock=lambda: self.now
            ).drive_worker_recovery(
                key,
                holder=holder,
                expected_action_generation=expectation.action_generation,
            )
        self.assertEqual(result.status, ACTUATION_RECOVERED)
        self.assertEqual(self._phase(store, key), PARTICIPANT_REPLACED)
        ops = LiveBoundPairPreparationOps(
            repo_root=Path("/coordinator"), env={}, transaction_store=store
        )
        self.assertTrue(ops._finish(key, expectation, holder, port))
        self.assertEqual(store.get(key).phase, PHASE_COMPLETED)


if __name__ == "__main__":
    unittest.main()
