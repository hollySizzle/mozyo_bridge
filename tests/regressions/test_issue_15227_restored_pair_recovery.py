"""Regression coverage for post-reboot active-pair recovery (Redmine #15227)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.herdr_identity_attestation import ATTEST_OK
from mozyo_bridge.core.state.replacement_preservation import PreservationObservation
from mozyo_bridge.core.state.replacement_transaction import (
    CAS_ACTION_MISMATCH,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_reapproval import (
    reapprove_zero_effect_transaction,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (
    TABLE as REPLACEMENT_TRANSACTION_TABLE,
)
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_LAUNCH_OWED,
    PHASE_CLAIMED,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E501
    EvidencePlanning,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery import (  # noqa: E501
    PairReplacementResult,
    RestoredPairRecoveryRequest,
    SublaneRestoredPairRecoveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_observation import (  # noqa: E501
    _row_runtime_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    GATE_RESTORED_PAIR_RECOVERY_OWNER_APPROVAL,
    ISSUER_COORDINATOR,
    RESTORED_PAIR_RECOVERY_APPROVAL_RULING,
    contract_ruling_pointer,
    contract_writer_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT,
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    APPROVAL_DEGRADED,
    APPROVAL_HEALTHY,
    BLOCK_ATTESTATION_UNREADABLE,
    BLOCK_DEFAULT_LANE,
    BLOCK_PAIR_HEALTHY,
    BLOCK_PAIR_INCOMPLETE,
    BLOCK_SLOT_BUSY,
    BLOCK_SLOT_RUNTIME_NOT_SETTLED,
    BLOCK_SUPERSEDE_NOT_READY,
    SLOT_GATEWAY,
    SLOT_WORKER,
    STATUS_COMPLETED,
    STATUS_REFUSED,
    RestoredPairPlan,
    RestoredSlot,
    restored_pair_approval_operation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    CLOSE_DONE,
    LAUNCH_DONE,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)


def _slot(
    role: str,
    *,
    cwd_matches: bool,
    locator: str,
    runtime_state: str = RUNTIME_AWAITING_INPUT,
) -> RestoredSlot:
    provider = "codex" if role == SLOT_GATEWAY else "claude"
    return RestoredSlot(
        slot_role=role,
        provider=provider,
        assigned_name=f"managed-{provider}",
        locator=locator,
        revision="12",
        identity_matches=True,
        inventory_generation_matches=True,
        runtime_state=runtime_state,
        cwd_matches=cwd_matches,
        attestation_state=ATTEST_OK,
        attestation_readable=True,
    )


def _plan(
    *,
    gateway_cwd: bool = False,
    worker_cwd: bool = False,
    lane: str = "issue_15227_post_reboot_exact_relaunch",
) -> RestoredPairPlan:
    return RestoredPairPlan(
        issue="15227",
        lane=lane,
        workspace_id="workspace-a",
        worktree_identity="wt_exact",
        branch="issue_15227_post_reboot_exact_relaunch",
        head="a" * 40,
        lane_revision="7",
        lane_generation="3",
        lifecycle_current=True,
        worktree_authority_current=True,
        worktree_authority_reason="ok",
        allow_pending_composer_loss=True,
        gateway=_slot(SLOT_GATEWAY, cwd_matches=gateway_cwd, locator="pane-g"),
        worker=_slot(SLOT_WORKER, cwd_matches=worker_cwd, locator="pane-w"),
    )


class _Ops:
    def __init__(self, plan: RestoredPairPlan) -> None:
        self.plan = plan
        self.progressed_replay = False
        self.approved = False
        self.replace_calls = 0

    def observe(self, _request):
        return self.plan

    def transaction_is_progressed_replay(self, _request, _plan) -> bool:
        return self.progressed_replay

    def approval_verified(self, _request, _plan) -> bool:
        return self.approved

    def replace_pair(self, _request, _plan) -> PairReplacementResult:
        self.replace_calls += 1
        return PairReplacementResult(True, phase="completed", revision=19, detail="done")


def _request(plan: RestoredPairPlan) -> RestoredPairRecoveryRequest:
    return RestoredPairRecoveryRequest(
        issue=plan.issue,
        lane=plan.lane,
        journal="102900",
        action_id=plan.action_id,
        action_generation=plan.action_generation,
        allow_pending_composer_loss=True,
        gateway_assigned_name=plan.gateway.assigned_name,
        gateway_locator=plan.gateway.locator,
        gateway_revision=plan.gateway.revision,
        worker_assigned_name=plan.worker.assigned_name,
        worker_locator=plan.worker.locator,
        worker_revision=plan.worker.revision,
        gateway_approval_health=plan.gateway.approval_health,
        worker_approval_health=plan.worker.approval_health,
        supersede=plan.supersede_requested,
        supersedes_generation=plan.supersedes_generation,
        supersedes_journal=plan.supersedes_journal,
        supersedes_revision=plan.supersedes_revision,
    )


class RestoredPairDecisionTests(unittest.TestCase):
    def test_reboot_restored_pair_with_wrong_cwd_is_approval_ready(self) -> None:
        plan = _plan(gateway_cwd=False, worker_cwd=False)
        self.assertTrue(plan.may_recover)
        self.assertEqual(plan.blocked_reasons, ())

    def test_green_pair_is_not_replaced(self) -> None:
        plan = _plan(gateway_cwd=True, worker_cwd=True)
        self.assertFalse(plan.may_recover)
        self.assertIn(BLOCK_PAIR_HEALTHY, plan.blocked_reasons)

    def test_default_lane_is_never_recovery_eligible(self) -> None:
        plan = _plan(lane="default")
        self.assertFalse(plan.may_recover)
        self.assertIn(BLOCK_DEFAULT_LANE, plan.blocked_reasons)

    def test_only_explicit_settled_runtime_is_recovery_eligible(self) -> None:
        plan = _plan()
        for state in (RUNTIME_UNKNOWN, "blocked"):
            with self.subTest(state=state):
                observed = replace(
                    plan,
                    gateway=replace(plan.gateway, runtime_state=state),
                )
                self.assertIn(
                    BLOCK_SLOT_RUNTIME_NOT_SETTLED, observed.blocked_reasons
                )
        busy = replace(
            plan, gateway=replace(plan.gateway, runtime_state=RUNTIME_BUSY)
        )
        self.assertIn(BLOCK_SLOT_BUSY, busy.blocked_reasons)
        ended = replace(
            plan, worker=replace(plan.worker, runtime_state=RUNTIME_TURN_ENDED)
        )
        self.assertTrue(ended.may_recover)

    def test_runtime_row_mapping_fails_closed(self) -> None:
        unknown_rows = (
            None,
            {},
            {"agent_status": None},
            {"agent_status": "unknown"},
            {"agent_status": "novel"},
            {"agent_status": 7},
        )
        for row in unknown_rows:
            with self.subTest(row=row):
                self.assertEqual(_row_runtime_state(row), RUNTIME_UNKNOWN)
        self.assertEqual(
            _row_runtime_state({"agent_status": "idle"}), RUNTIME_AWAITING_INPUT
        )
        self.assertEqual(
            _row_runtime_state({"agent_status": "done"}), RUNTIME_TURN_ENDED
        )

    def test_unreadable_attestation_is_not_bad_generation_proof(self) -> None:
        plan = _plan()
        gateway = replace(plan.gateway, attestation_readable=False)
        plan = replace(plan, gateway=gateway)
        self.assertIn(BLOCK_ATTESTATION_UNREADABLE, plan.blocked_reasons)

    def test_action_id_changes_when_exact_old_generation_changes(self) -> None:
        plan = _plan()
        recycled = replace(
            plan, worker=replace(plan.worker, locator="pane-w-new", revision="13")
        )
        self.assertNotEqual(plan.action_id, recycled.action_id)


class RestoredPairUseCaseTests(unittest.TestCase):
    def test_preflight_renders_exact_owner_approval_and_has_zero_effect(self) -> None:
        plan = _plan()
        ops = _Ops(plan)
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(_request(plan))
        self.assertFalse(outcome.executed)
        self.assertIn(
            f"gate={RESTORED_PAIR_RECOVERY_APPROVAL_GATE}",
            outcome.required_approval_marker,
        )
        self.assertIn(
            f"effect={RESTORED_PAIR_RECOVERY_APPROVAL_EFFECT}",
            outcome.required_approval_marker,
        )
        self.assertEqual(ops.replace_calls, 0)

    def test_execute_requires_verified_exact_approval(self) -> None:
        plan = _plan()
        ops = _Ops(plan)
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertEqual(ops.replace_calls, 0)

    def test_execute_requires_both_owner_preflight_health_pins(self) -> None:
        plan = _plan()
        ops = _Ops(plan)
        ops.approved = True
        request = replace(_request(plan), gateway_approval_health="")
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            request, execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn("health classification", outcome.detail)
        self.assertEqual(ops.replace_calls, 0)

    def test_default_lane_preflight_has_no_approval_marker_or_effect(self) -> None:
        plan = _plan(lane="default")
        ops = _Ops(plan)
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(_request(plan))
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.required_approval_marker, "")
        self.assertIn(BLOCK_DEFAULT_LANE, outcome.plan.blocked_reasons)
        self.assertEqual(ops.replace_calls, 0)

    def test_execute_replaces_pair_once_after_approval(self) -> None:
        plan = _plan()
        ops = _Ops(plan)
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED)
        self.assertEqual(outcome.phase, "completed")
        self.assertEqual(ops.replace_calls, 1)
        self.assertFalse(outcome.conversation_resume_guaranteed)

    def test_execute_refuses_when_degraded_slot_heals_after_owner_preflight(
        self,
    ) -> None:
        approved = _plan()
        healed = replace(
            approved,
            gateway=replace(approved.gateway, cwd_matches=True),
        )
        self.assertEqual(approved.action_id, healed.action_id)
        self.assertNotEqual(
            restored_pair_approval_operation(approved),
            restored_pair_approval_operation(healed),
        )
        ops = _Ops(healed)
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(approved), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn("health changed", outcome.detail)
        self.assertEqual(ops.replace_calls, 0)

    def test_default_lane_execute_is_zero_effect_even_for_progressed_replay(self) -> None:
        plan = _plan(lane="default")
        ops = _Ops(plan)
        ops.progressed_replay = True
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertEqual(ops.replace_calls, 0)

    def test_planned_transaction_does_not_bypass_healthy_pair(self) -> None:
        plan = _plan(gateway_cwd=True, worker_cwd=True)
        ops = _Ops(plan)
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn(BLOCK_PAIR_HEALTHY, outcome.plan.blocked_reasons)
        self.assertEqual(ops.replace_calls, 0)

    def test_progressed_replay_does_not_bypass_busy_slot(self) -> None:
        plan = _plan()
        plan = replace(
            plan,
            gateway=replace(plan.gateway, runtime_state=RUNTIME_BUSY),
        )
        ops = _Ops(plan)
        ops.progressed_replay = True
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertEqual(ops.replace_calls, 0)

    def test_partial_transaction_may_resume_with_old_generation_pins(self) -> None:
        plan = _plan()
        gateway = replace(plan.gateway, inventory_generation_matches=False)
        partial = replace(plan, gateway=gateway)
        self.assertIn(BLOCK_PAIR_INCOMPLETE, partial.blocked_reasons)
        ops = _Ops(partial)
        ops.progressed_replay = True
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(partial), execute=True
        )
        self.assertEqual(outcome.status, STATUS_COMPLETED)
        self.assertEqual(ops.replace_calls, 1)

    def test_partial_transaction_still_refuses_unreadable_attestation_store(self) -> None:
        plan = _plan()
        partial = replace(
            plan,
            gateway=replace(
                plan.gateway,
                inventory_generation_matches=False,
                attestation_readable=False,
            ),
        )
        ops = _Ops(partial)
        ops.progressed_replay = True
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(partial), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertEqual(ops.replace_calls, 0)


class RestoredPairReplayAdmissionTests(unittest.TestCase):
    @staticmethod
    def _declare(store, plan, request):
        key = ReplacementTransactionKey(plan.workspace_id, plan.action_id)
        participants = [
            ParticipantPin(
                lane_id=plan.lane,
                role=slot.provider,
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                old_locator=slot.locator,
                lane_revision=plan.lane_revision,
                lane_generation=plan.lane_generation,
            )
            for slot in plan.slots
        ]
        result = store.plan_transaction(
            key,
            action_generation=request.action_generation,
            decision=DecisionPointer("redmine", plan.issue, request.journal),
            continuation=ContinuationPointer(
                "redmine",
                plan.issue,
                request.journal,
                RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
                "pair_relaunch_no_dispatch",
            ),
            participants=participants,
        )
        if not result.applied:
            raise AssertionError(result.reason)
        return key, participants

    def test_exact_planned_row_does_not_bypass_healthy_zero_close(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        plan = _plan(gateway_cwd=True, worker_cwd=True)
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(path=Path(temp) / "replacement.sqlite")
            self._declare(store, plan, request)
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store
            )
            self.assertFalse(ops.transaction_is_progressed_replay(request, plan))
            with (
                mock.patch.object(ops, "observe", return_value=plan),
                mock.patch.object(ops, "approval_verified", return_value=True),
                mock.patch.object(ops, "replace_pair") as replace_pair,
            ):
                outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
                    request, execute=True
                )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        replace_pair.assert_not_called()

    def test_only_exact_participant_progress_is_replay_authority(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(path=Path(temp) / "replacement.sqlite")
            key, participants = self._declare(store, plan, request)
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store
            )
            record = store.get(key)
            assert record is not None
            claim = store.claim(
                key,
                expected_revision=record.revision,
                expected_action_generation=request.action_generation,
                holder=request.holder,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(claim.applied, claim.reason)
            for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
                record = store.get(key)
                assert record is not None
                moved = store.transition_phase(
                    key,
                    expected_revision=record.revision,
                    expected_action_generation=request.action_generation,
                    target=phase,
                    holder=request.holder,
                    now="2026-08-10T00:00:00+00:00",
                )
                self.assertTrue(moved.applied, moved.reason)
            record = store.get(key)
            assert record is not None
            progressed = store.transition_participant(
                key,
                expected_revision=record.revision,
                expected_action_generation=request.action_generation,
                identity=participants[0].identity,
                target=PARTICIPANT_LAUNCH_OWED,
                holder=request.holder,
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(progressed.applied, progressed.reason)
            self.assertTrue(ops.transaction_is_progressed_replay(request, plan))
            partial = replace(
                plan,
                gateway=replace(
                    plan.gateway, inventory_generation_matches=False
                ),
            )
            self.assertIn(BLOCK_PAIR_INCOMPLETE, partial.blocked_reasons)
            with (
                mock.patch.object(ops, "observe", return_value=partial),
                mock.patch.object(ops, "approval_verified", return_value=True),
                mock.patch.object(
                    ops,
                    "replace_pair",
                    return_value=PairReplacementResult(
                        True, phase="completed", revision=8, detail="done"
                    ),
                ) as replace_pair,
            ):
                outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
                    request, execute=True
                )
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            replace_pair.assert_called_once()
            self.assertFalse(
                ops.transaction_is_progressed_replay(
                    replace(request, journal="102901"), plan
                )
            )
            self.assertFalse(
                ops.transaction_is_progressed_replay(
                    replace(request, action_generation=2), plan
                )
            )
            moved_plan = replace(
                plan,
                gateway=replace(plan.gateway, locator="different-old-locator"),
            )
            self.assertFalse(
                ops.transaction_is_progressed_replay(request, moved_plan)
            )


class RestoredPairReapprovalCasTests(unittest.TestCase):
    @staticmethod
    def _new_pointers(plan, journal="102902"):
        return (
            DecisionPointer("redmine", plan.issue, journal),
            ContinuationPointer(
                "redmine",
                plan.issue,
                journal,
                RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
                "pair_relaunch_no_dispatch",
            ),
        )

    @classmethod
    def _apply(cls, store, key, plan, request, **overrides):
        current = store.get(key)
        assert current is not None
        decision, continuation = cls._new_pointers(
            plan, overrides.pop("new_journal", "102902")
        )
        return reapprove_zero_effect_transaction(
            store,
            key,
            expected_revision=overrides.pop(
                "expected_revision", current.revision
            ),
            expected_action_generation=overrides.pop(
                "expected_action_generation", current.action_generation
            ),
            expected_journal=overrides.pop(
                "expected_journal", request.journal
            ),
            new_action_generation=overrides.pop(
                "new_action_generation", current.action_generation + 1
            ),
            decision=overrides.pop("decision", decision),
            continuation=overrides.pop("continuation", continuation),
            now=overrides.pop("now", "2026-08-10T00:00:00+00:00"),
            **overrides,
        )

    def test_exact_next_generation_reanchors_journal_and_preserves_manifest(
        self,
    ) -> None:
        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            before = store.get(key)
            assert before is not None
            outcome = self._apply(store, key, plan, request)
            after = store.get(key)

        self.assertTrue(outcome.applied, outcome.reason)
        assert after is not None
        self.assertEqual(after.action_generation, 2)
        self.assertEqual(after.revision, before.revision + 1)
        self.assertEqual(after.decision.journal_id, "102902")
        self.assertEqual(after.continuation.journal_id, "102902")
        self.assertEqual(after.participants_manifest, before.participants_manifest)
        self.assertEqual(after.phase, "planned")
        self.assertEqual(after.lease_holder, "")

    def test_reapproval_refuses_absent_stale_generation_revision_and_scope(
        self,
    ) -> None:
        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            missing = reapprove_zero_effect_transaction(
                store,
                ReplacementTransactionKey(plan.workspace_id, plan.action_id),
                expected_revision=1,
                expected_action_generation=1,
                expected_journal=request.journal,
                new_action_generation=2,
                decision=self._new_pointers(plan)[0],
                continuation=self._new_pointers(plan)[1],
                now="2026-08-10T00:00:00+00:00",
            )
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            cases = {
                "same_generation": (
                    {"new_action_generation": 1},
                    CAS_GENERATION_MISMATCH,
                ),
                "skipped_generation": (
                    {"new_action_generation": 3},
                    CAS_GENERATION_MISMATCH,
                ),
                "old_generation": (
                    {"expected_action_generation": 2},
                    CAS_GENERATION_MISMATCH,
                ),
                "old_revision": ({"expected_revision": 2}, CAS_STALE_REVISION),
                "old_journal": (
                    {"expected_journal": "102899"},
                    CAS_ACTION_MISMATCH,
                ),
                "new_issue": (
                    {
                        "decision": DecisionPointer(
                            "redmine", "15228", "102902"
                        )
                    },
                    CAS_ACTION_MISMATCH,
                ),
                "new_gate": (
                    {
                        "continuation": ContinuationPointer(
                            "redmine",
                            plan.issue,
                            "102902",
                            "other_gate",
                            "pair_relaunch_no_dispatch",
                        )
                    },
                    CAS_ACTION_MISMATCH,
                ),
            }
            observed = {}
            for label, (kwargs, _expected) in cases.items():
                observed[label] = self._apply(
                    store, key, plan, request, **kwargs
                ).reason

        self.assertEqual(missing.reason, CAS_NOT_FOUND)
        for label, (_kwargs, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(observed[label], expected)

    def test_live_lease_and_actuated_participant_are_immutable_fences(self) -> None:
        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            current = store.get(key)
            assert current is not None
            claimed = store.claim(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                holder=request.holder,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                now="2026-08-10T00:00:00+00:00",
            )
            live_lease = self._apply(
                store, key, plan, request, now="2026-08-10T00:00:00+00:00"
            )
            for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
                current = store.get(key)
                assert current is not None
                moved = store.transition_phase(
                    key,
                    expected_revision=current.revision,
                    expected_action_generation=1,
                    target=phase,
                    holder=request.holder,
                    now="2026-08-10T00:00:00+00:00",
                )
                self.assertTrue(moved.applied, moved.reason)
            current = store.get(key)
            assert current is not None
            progressed = store.transition_participant(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                identity=participants[0].identity,
                target=PARTICIPANT_LAUNCH_OWED,
                holder=request.holder,
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(progressed.applied, progressed.reason)
            actuated = self._apply(
                store, key, plan, request, now="2100-01-01T00:00:00+00:00"
            )

        self.assertTrue(claimed.applied, claimed.reason)
        self.assertEqual(live_lease.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(actuated.reason, CAS_UNEXPECTED_STATE)

    def test_reanchor_invalidates_old_generation_executor(self) -> None:
        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            reanchored = self._apply(store, key, plan, request)
            current = store.get(key)
            assert current is not None
            stale_claim = store.claim(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                holder=request.holder,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                now="2026-08-10T00:00:01+00:00",
            )

        self.assertTrue(reanchored.applied, reanchored.reason)
        self.assertEqual(stale_claim.reason, CAS_GENERATION_MISMATCH)

    def test_expired_claimed_zero_effect_row_may_return_to_planned(self) -> None:
        plan = _plan()
        request = _request(plan)
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            current = store.get(key)
            assert current is not None
            claimed = store.claim(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                holder=request.holder,
                lease_expires_at="2026-08-10T00:00:01+00:00",
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(claimed.applied, claimed.reason)
            current = store.get(key)
            assert current is not None
            moved = store.transition_phase(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                target=PHASE_CLAIMED,
                holder=request.holder,
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(moved.applied, moved.reason)
            reanchored = self._apply(
                store,
                key,
                plan,
                request,
                now="2026-08-10T00:00:02+00:00",
            )
            after = store.get(key)

        self.assertTrue(reanchored.applied, reanchored.reason)
        assert after is not None
        self.assertEqual(after.phase, "planned")
        self.assertEqual(after.action_generation, 2)
        self.assertTrue(all(pin.phase == "close_owed" for pin in after.participants))

    def test_close_effect_crash_window_refuses_both_supersede_rails(self) -> None:
        """External close success before participant CAS is never called zero-effect."""

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
            ReplacementActuatorUseCase,
        )

        plan = _plan()
        request = _request(plan)

        class CrashAfterClosePort:
            def __init__(self) -> None:
                self.close_calls = 0

            def observe_old_slot(self, _pin):
                return OLD_SLOT_PRESENT

            def observe_preservation(self, _pin):
                return PreservationObservation(
                    identity_matches=True, attestation_fresh=True
                )

            def close_exact_generation(self, _pin):
                self.close_calls += 1
                raise SystemExit("simulated crash after external close")

            def launch_action_bound(self, _action_id, _pin):
                raise AssertionError("launch must not run")

            def verify_attestation(self, _action_id, _pin):
                raise AssertionError("verify must not run")

        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, participants = RestoredPairReplayAdmissionTests._declare(
                store, plan, request
            )
            port = CrashAfterClosePort()
            actuator = ReplacementActuatorUseCase(
                store,
                port,
                clock=lambda: "2026-08-10T00:00:00+00:00",
            )
            with self.assertRaises(SystemExit):
                actuator.drive_worker_recovery(
                    key,
                    holder=request.holder,
                    expected_action_generation=1,
                )
            crashed = store.get(key)
            assert crashed is not None
            self.assertEqual(crashed.phase, PHASE_REPLACING_NONSELF)
            self.assertTrue(
                all(pin.phase == "close_owed" for pin in crashed.participants)
            )

            generic = store.supersede_transaction(
                key,
                new_action_generation=2,
                decision=crashed.decision,
                continuation=crashed.continuation,
                participants=participants,
                now="2100-01-01T00:00:00+00:00",
            )
            decision, continuation = self._new_pointers(plan, "102902")
            dedicated = reapprove_zero_effect_transaction(
                store,
                key,
                expected_revision=crashed.revision,
                expected_action_generation=1,
                expected_journal=request.journal,
                new_action_generation=2,
                decision=decision,
                continuation=continuation,
                now="2100-01-01T00:00:00+00:00",
            )
            after = store.get(key)

        self.assertEqual(port.close_calls, 1)
        self.assertEqual(generic.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(dedicated.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(after, crashed)

    def test_corrupt_pointer_and_manifest_fail_closed_without_write(self) -> None:
        plan = _plan()
        request = _request(plan)
        for column, value, expected in (
            ("decision_journal", "", CAS_ACTION_MISMATCH),
            ("participants_manifest", "not-json", CAS_UNEXPECTED_STATE),
        ):
            with self.subTest(column=column), TemporaryDirectory() as temp:
                store = ReplacementTransactionStore(
                    path=Path(temp) / "replacement.sqlite"
                )
                key, _participants = RestoredPairReplayAdmissionTests._declare(
                    store, plan, request
                )
                conn = store._connect()
                try:
                    conn.execute(
                        f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET {column} = ? "
                        "WHERE workspace_id = ? AND action_id = ?",
                        (value, key.workspace_id, key.action_id),
                    )
                finally:
                    conn.close()
                before = store.get(key)
                assert before is not None
                result = self._apply(store, key, plan, request)
                after = store.get(key)
                assert after is not None
                self.assertEqual(result.reason, expected)
                self.assertEqual(after.revision, before.revision)
                self.assertEqual(after.action_generation, 1)


class RestoredPairSupersedeCompositionTests(unittest.TestCase):
    @staticmethod
    def _derive(ops, site, request, base):
        with mock.patch.object(
            site.LiveRestoredPairObservation, "observe", return_value=base
        ):
            return ops.observe(request)

    def _setup(self, temp):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        base = _plan()
        old_request = _request(base)
        store = ReplacementTransactionStore(
            path=Path(temp) / "replacement.sqlite"
        )
        key, _participants = RestoredPairReplayAdmissionTests._declare(
            store, base, old_request
        )
        ops = site.LiveRestoredPairRecoveryOps(
            repo_root=Path(temp), transaction_store=store
        )
        preflight_request = replace(
            old_request,
            journal="",
            action_id="",
            action_generation=0,
            supersede=True,
        )
        plan = self._derive(ops, site, preflight_request, base)
        new_request = replace(_request(plan), journal="102902")
        return site, base, store, key, ops, plan, new_request

    def test_preflight_derives_exact_next_generation_and_old_cas_pins(self) -> None:
        with TemporaryDirectory() as temp:
            _site, _base, _store, _key, _ops, plan, request = self._setup(temp)

        self.assertTrue(plan.supersede_requested)
        self.assertEqual(plan.action_generation, 2)
        self.assertEqual(plan.supersedes_generation, 1)
        self.assertEqual(plan.supersedes_journal, "102900")
        self.assertEqual(plan.supersedes_revision, 1)
        self.assertTrue(plan.may_recover)
        operation = restored_pair_approval_operation(plan)
        self.assertTrue(operation["supersede"])
        self.assertEqual(operation["supersedes_generation"], 1)
        self.assertEqual(operation["supersedes_journal"], "102900")
        self.assertEqual(request.action_generation, 2)

    def test_supersede_preflight_without_existing_row_is_blocked(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        base = _plan()
        request = replace(
            _request(base),
            journal="",
            action_id="",
            action_generation=0,
            supersede=True,
        )
        with TemporaryDirectory() as temp:
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp),
                transaction_store=ReplacementTransactionStore(
                    path=Path(temp) / "replacement.sqlite"
                ),
            )
            plan = self._derive(ops, site, request, base)

        self.assertFalse(plan.may_recover)
        self.assertIn(BLOCK_SUPERSEDE_NOT_READY, plan.blocked_reasons)
        self.assertEqual(plan.supersedes_generation, 0)

    def test_supersede_preflight_refuses_ambiguous_replacing_row(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        base = _plan()
        old_request = _request(base)
        preflight_request = replace(
            old_request,
            journal="",
            action_id="",
            action_generation=0,
            supersede=True,
        )
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(
                path=Path(temp) / "replacement.sqlite"
            )
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, base, old_request
            )
            current = store.get(key)
            assert current is not None
            claimed = store.claim(
                key,
                expected_revision=current.revision,
                expected_action_generation=1,
                holder=old_request.holder,
                lease_expires_at="2000-01-01T00:00:01+00:00",
                now="2000-01-01T00:00:00+00:00",
            )
            self.assertTrue(claimed.applied, claimed.reason)
            for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
                current = store.get(key)
                assert current is not None
                moved = store.transition_phase(
                    key,
                    expected_revision=current.revision,
                    expected_action_generation=1,
                    target=phase,
                    holder=old_request.holder,
                    now="2000-01-01T00:00:00+00:00",
                )
                self.assertTrue(moved.applied, moved.reason)
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store
            )
            with mock.patch.object(
                site.LiveRestoredPairObservation, "observe", return_value=base
            ):
                plan = ops.observe(preflight_request)

        self.assertFalse(plan.may_recover)
        self.assertIn(BLOCK_SUPERSEDE_NOT_READY, plan.blocked_reasons)
        self.assertEqual(plan.supersedes_generation, 0)

    def test_cas_success_crash_rerun_adopts_new_header_without_resupersede(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            site, base, store, key, ops, plan, request = self._setup(temp)
            stopped = mock.Mock(
                status="simulated_before_effect_stop",
                phase="planned",
                revision=2,
                detail="simulated",
            )
            with (
                mock.patch.object(ops, "observe", return_value=plan),
                mock.patch.object(
                    site.ReplacementActuatorUseCase,
                    "drive_worker_recovery",
                    return_value=stopped,
                ),
            ):
                first = ops.replace_pair(request, plan)
            reanchored = store.get(key)
            assert reanchored is not None
            replay_plan = self._derive(ops, site, request, base)
            with (
                mock.patch.object(
                    site,
                    "reapprove_zero_effect_transaction",
                    side_effect=AssertionError("exact replay must not re-supersede"),
                ),
                mock.patch.object(
                    site.ReplacementActuatorUseCase,
                    "drive_worker_recovery",
                    return_value=stopped,
                ),
            ):
                replay = ops.replace_pair(request, replay_plan)

        self.assertFalse(first.completed)
        self.assertEqual(reanchored.action_generation, 2)
        self.assertEqual(reanchored.decision.journal_id, "102902")
        self.assertEqual(replay_plan.action_generation, 2)
        self.assertEqual(replay_plan.supersedes_generation, 1)
        self.assertFalse(replay.completed)
        self.assertNotIn("different replacement authority", replay.detail)

    def test_fresh_health_drift_refuses_before_reapproval_cas(self) -> None:
        with TemporaryDirectory() as temp:
            _site, _base, store, key, ops, plan, request = self._setup(temp)
            drifted = replace(
                plan,
                gateway=replace(plan.gateway, cwd_matches=True),
            )
            with mock.patch.object(ops, "observe", return_value=drifted):
                result = ops.replace_pair(request, plan)
            current = store.get(key)

        self.assertFalse(result.completed)
        self.assertIn("fresh pair requalification failed", result.detail)
        assert current is not None
        self.assertEqual(current.action_generation, 1)
        self.assertEqual(current.decision.journal_id, "102900")

    def test_new_generation_header_remains_partial_replay_authority(self) -> None:
        with TemporaryDirectory() as temp:
            site, _base, store, key, ops, plan, request = self._setup(temp)
            decision, continuation = RestoredPairReapprovalCasTests._new_pointers(
                plan, request.journal
            )
            changed = reapprove_zero_effect_transaction(
                store,
                key,
                expected_revision=plan.supersedes_revision,
                expected_action_generation=plan.supersedes_generation,
                expected_journal=plan.supersedes_journal,
                new_action_generation=plan.action_generation,
                decision=decision,
                continuation=continuation,
                now="2026-08-10T00:00:00+00:00",
            )
            self.assertTrue(changed.applied, changed.reason)
            current = store.get(key)
            assert current is not None
            claimed = store.claim(
                key,
                expected_revision=current.revision,
                expected_action_generation=2,
                holder=request.holder,
                lease_expires_at="2099-01-01T00:00:00+00:00",
                now="2026-08-10T00:00:01+00:00",
            )
            self.assertTrue(claimed.applied, claimed.reason)
            for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
                current = store.get(key)
                assert current is not None
                moved = store.transition_phase(
                    key,
                    expected_revision=current.revision,
                    expected_action_generation=2,
                    target=phase,
                    holder=request.holder,
                    now="2026-08-10T00:00:01+00:00",
                )
                self.assertTrue(moved.applied, moved.reason)
            current = store.get(key)
            assert current is not None
            progressed_pin = current.participants[0]
            moved = store.transition_participant(
                key,
                expected_revision=current.revision,
                expected_action_generation=2,
                identity=progressed_pin.identity,
                target=PARTICIPANT_LAUNCH_OWED,
                holder=request.holder,
                now="2026-08-10T00:00:01+00:00",
            )
            self.assertTrue(moved.applied, moved.reason)
            partial = (
                replace(
                    plan,
                    gateway=replace(
                        plan.gateway, inventory_generation_matches=False
                    ),
                )
                if progressed_pin.assigned_name == plan.gateway.assigned_name
                else replace(
                    plan,
                    worker=replace(
                        plan.worker, inventory_generation_matches=False
                    ),
                )
            )
            self.assertTrue(
                ops.transaction_is_progressed_replay(request, partial)
            )
            self.assertEqual(partial.action_generation, 2)
            self.assertEqual(
                partial.supersedes_journal, plan.supersedes_journal
            )


class RestoredPairApprovalAuthorityTests(unittest.TestCase):
    def test_gate_has_coordinator_writer_and_exact_design_ruling(self) -> None:
        self.assertEqual(
            GATE_RESTORED_PAIR_RECOVERY_OWNER_APPROVAL,
            RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
        )
        self.assertEqual(
            contract_writer_role(RESTORED_PAIR_RECOVERY_APPROVAL_GATE),
            ISSUER_COORDINATOR,
        )
        self.assertEqual(
            contract_ruling_pointer(RESTORED_PAIR_RECOVERY_APPROVAL_GATE),
            RESTORED_PAIR_RECOVERY_APPROVAL_RULING,
        )


class RestoredPairCliContractTests(unittest.TestCase):
    def test_public_parser_carries_exact_owner_preflight_health_pins(self) -> None:
        args = build_parser().parse_args(
            [
                "sublane",
                "recover-restored-pair",
                "--issue",
                "15227",
                "--lane",
                "issue_15227_post_reboot_exact_relaunch",
                "--gateway-approval-health",
                APPROVAL_DEGRADED,
                "--worker-approval-health",
                APPROVAL_HEALTHY,
                "--supersede",
                "--supersedes-generation",
                "1",
                "--supersedes-journal",
                "102900",
                "--supersedes-revision",
                "7",
                "--execute",
            ]
        )
        self.assertEqual(args.gateway_approval_health, APPROVAL_DEGRADED)
        self.assertEqual(args.worker_approval_health, APPROVAL_HEALTHY)
        self.assertTrue(args.supersede)
        self.assertEqual(args.supersedes_generation, 1)
        self.assertEqual(args.supersedes_journal, "102900")
        self.assertEqual(args.supersedes_revision, 7)
        self.assertTrue(args.execute)

    def test_preflight_text_and_json_expose_copyable_slot_health_pins(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_cli import (  # noqa: E501
            format_restored_pair_recovery_text,
        )

        plan = _plan(gateway_cwd=False, worker_cwd=True)
        outcome = SublaneRestoredPairRecoveryUseCase(_Ops(plan)).run(
            _request(plan)
        )
        rendered = format_restored_pair_recovery_text(outcome)
        self.assertIn("gateway:", rendered)
        self.assertIn(f"approval_health={APPROVAL_DEGRADED}", rendered)
        self.assertIn("worker:", rendered)
        self.assertIn(f"approval_health={APPROVAL_HEALTHY}", rendered)
        slots = {
            slot["slot_role"]: slot
            for slot in outcome.as_payload()["plan"]["slots"]
        }
        self.assertEqual(slots[SLOT_GATEWAY]["approval_health"], APPROVAL_DEGRADED)
        self.assertEqual(slots[SLOT_WORKER]["approval_health"], APPROVAL_HEALTHY)


class RestoredPairCloseBoundaryTests(unittest.TestCase):
    @staticmethod
    def _identity(plan, slot):
        return (plan.lane, slot.provider, slot.provider, slot.assigned_name)

    @classmethod
    def _approval_health(cls, plan):
        return {
            cls._identity(plan, slot): slot.approval_health
            for slot in plan.slots
        }

    def test_full_pair_close_requalification_rejects_action_time_drift(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        approved = _plan()
        pending = {self._identity(approved, slot) for slot in approved.slots}
        cases = {
            "runtime_unknown": replace(
                approved,
                worker=replace(
                    approved.worker, runtime_state=RUNTIME_UNKNOWN
                ),
            ),
            "pair_became_healthy": replace(
                approved,
                gateway=replace(approved.gateway, cwd_matches=True),
                worker=replace(approved.worker, cwd_matches=True),
            ),
            "inventory_revision_moved": replace(
                approved,
                worker=replace(
                    approved.worker, inventory_generation_matches=False
                ),
            ),
            "head_moved": replace(approved, head="b" * 40),
            "lifecycle_moved": replace(approved, lifecycle_current=False),
            "worktree_moved": replace(
                approved, worktree_authority_current=False
            ),
        }
        for label, fresh in cases.items():
            with self.subTest(label=label):
                self.assertFalse(
                    site._fresh_pair_close_requalified(
                        approved,
                        fresh,
                        pending_identities=pending,
                        all_participants_close_owed=True,
                        approval_health_by_identity=self._approval_health(approved),
                    )
                )

    def test_progressed_replay_ignores_fresh_replacement_but_checks_pending_slot(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        approved = _plan()
        pending = {self._identity(approved, approved.gateway)}
        fresh = replace(
            approved,
            worker=replace(
                approved.worker,
                inventory_generation_matches=False,
                runtime_state=RUNTIME_UNKNOWN,
                attestation_readable=False,
            ),
        )
        self.assertTrue(
            site._fresh_pair_close_requalified(
                approved,
                fresh,
                pending_identities=pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(approved),
            )
        )
        pending_unknown = replace(
            fresh,
            gateway=replace(fresh.gateway, runtime_state=RUNTIME_UNKNOWN),
        )
        self.assertFalse(
            site._fresh_pair_close_requalified(
                approved,
                pending_unknown,
                pending_identities=pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(approved),
            )
        )
        pending_became_healthy = replace(
            fresh,
            gateway=replace(fresh.gateway, cwd_matches=True),
        )
        self.assertFalse(
            site._fresh_pair_close_requalified(
                approved,
                pending_became_healthy,
                pending_identities=pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(approved),
            )
        )

        # A sibling that was already healthy when the exact pair action was approved is
        # not a post-approval recovery.  Once the damaged participant has progressed, that
        # original healthy sibling remains eligible so partial convergence is not stranded.
        mixed_approved = _plan(gateway_cwd=True, worker_cwd=False)
        mixed_pending = {self._identity(mixed_approved, mixed_approved.gateway)}
        mixed_fresh = replace(
            mixed_approved,
            worker=replace(
                mixed_approved.worker,
                inventory_generation_matches=False,
                runtime_state=RUNTIME_UNKNOWN,
                attestation_readable=False,
            ),
        )
        self.assertTrue(
            site._fresh_pair_close_requalified(
                mixed_approved,
                mixed_fresh,
                pending_identities=mixed_pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(mixed_approved),
            )
        )

    def test_approval_health_pins_distinguish_original_health_from_later_heal(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        approved = _plan()
        pending = {self._identity(approved, approved.gateway)}
        healed = replace(
            approved,
            gateway=replace(approved.gateway, cwd_matches=True),
            worker=replace(
                approved.worker,
                inventory_generation_matches=False,
                runtime_state=RUNTIME_UNKNOWN,
                attestation_readable=False,
            ),
        )
        self.assertFalse(
            site._fresh_pair_close_requalified(
                approved,
                healed,
                pending_identities=pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(approved),
            )
        )

        originally_healthy = _plan(gateway_cwd=True, worker_cwd=False)
        pending = {self._identity(originally_healthy, originally_healthy.gateway)}
        replay = replace(
            originally_healthy,
            worker=replace(
                originally_healthy.worker,
                inventory_generation_matches=False,
                runtime_state=RUNTIME_UNKNOWN,
                attestation_readable=False,
            ),
        )
        self.assertTrue(
            site._fresh_pair_close_requalified(
                originally_healthy,
                replay,
                pending_identities=pending,
                all_participants_close_owed=False,
                approval_health_by_identity=self._approval_health(
                    originally_healthy
                ),
            )
        )
        self.assertEqual(
            self._approval_health(approved)[self._identity(approved, approved.gateway)],
            APPROVAL_DEGRADED,
        )
        self.assertEqual(
            self._approval_health(originally_healthy)[
                self._identity(originally_healthy, originally_healthy.gateway)
            ],
            APPROVAL_HEALTHY,
        )

    def test_first_close_refuses_healthy_to_degraded_post_approval_drift(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        approved = _plan(gateway_cwd=True, worker_cwd=False)
        pending = {self._identity(approved, slot) for slot in approved.slots}
        gateway_degraded = replace(
            approved,
            gateway=replace(approved.gateway, cwd_matches=False),
        )
        self.assertEqual(approved.action_id, gateway_degraded.action_id)
        self.assertTrue(gateway_degraded.may_recover)
        self.assertFalse(
            site._fresh_pair_close_requalified(
                approved,
                gateway_degraded,
                pending_identities=pending,
                all_participants_close_owed=True,
                approval_health_by_identity=self._approval_health(approved),
            )
        )

    def test_close_port_has_zero_effect_when_fresh_pair_fails_requalification(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        approved = _plan()
        request = _request(approved)
        participants = tuple(
            ParticipantPin(
                lane_id=approved.lane,
                role=slot.provider,
                provider=slot.provider,
                assigned_name=slot.assigned_name,
                old_locator=slot.locator,
                lane_revision=approved.lane_revision,
                lane_generation=approved.lane_generation,
            )
            for slot in approved.slots
        )

        class FakePort:
            def __init__(self):
                self.close_calls = 0

            def observe_old_slot(self, _pin):
                return OLD_SLOT_PRESENT

            def observe_preservation(self, _pin):
                return PreservationObservation(
                    identity_matches=True, attestation_fresh=True
                )

            def close_exact_generation(self, _pin):
                self.close_calls += 1
                return CLOSE_DONE

            def launch_action_bound(self, _action_id, _pin):
                return LAUNCH_DONE

            def verify_attestation(self, _action_id, _pin):
                return ATTEST_BOUND

        fresh = replace(
            approved,
            worker=replace(approved.worker, runtime_state=RUNTIME_UNKNOWN),
        )
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(path=Path(temp) / "replacement.sqlite")
            key, _participants = RestoredPairReplayAdmissionTests._declare(
                store, approved, request
            )
            boundary = site._PairCloseBoundary(
                store=store,
                key=key,
                request=request,
                approved_plan=approved,
                observe=lambda: fresh,
            )
            fake_port = FakePort()
            actuator = site.ReplacementActuatorUseCase(
                store,
                fake_port,
                close_authority=boundary,
            )
            result = actuator.drive_worker_recovery(
                key,
                holder=request.holder,
                expected_action_generation=request.action_generation,
            )

        self.assertEqual(result.detail, "close_authority_moved")
        self.assertEqual(fake_port.close_calls, 0)


class RestoredPairTransactionCompositionTests(unittest.TestCase):
    @staticmethod
    def _seed_worker_launch_owed(store, plan, request):
        key, participants = RestoredPairReplayAdmissionTests._declare(
            store, plan, request
        )
        record = store.get(key)
        assert record is not None
        claimed = store.claim(
            key,
            expected_revision=record.revision,
            expected_action_generation=request.action_generation,
            holder=request.holder,
            lease_expires_at="2099-01-01T00:00:00+00:00",
            now="2026-08-10T00:00:00+00:00",
        )
        if not claimed.applied:
            raise AssertionError(claimed.reason)
        for phase in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
            record = store.get(key)
            assert record is not None
            moved = store.transition_phase(
                key,
                expected_revision=record.revision,
                expected_action_generation=request.action_generation,
                target=phase,
                holder=request.holder,
                now="2026-08-10T00:00:00+00:00",
            )
            if not moved.applied:
                raise AssertionError(moved.reason)
        record = store.get(key)
        assert record is not None
        moved = store.transition_participant(
            key,
            expected_revision=record.revision,
            expected_action_generation=request.action_generation,
            identity=participants[1].identity,
            target=PARTICIPANT_LAUNCH_OWED,
            holder=request.holder,
            now="2026-08-10T00:00:00+00:00",
        )
        if not moved.applied:
            raise AssertionError(moved.reason)
        return key, participants

    def _exercise_worker_launch_replay(self, worker_attestation: str):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        plan = _plan()
        request = _request(plan)

        class FakePort:
            def __init__(self) -> None:
                self.closed: list[str] = []
                self.launched: list[str] = []
                self.verified: list[str] = []

            def observe_old_slot(self, _pin):
                return OLD_SLOT_PRESENT

            def observe_preservation(self, _pin):
                return PreservationObservation(
                    identity_matches=True, attestation_fresh=True
                )

            def close_exact_generation(self, pin):
                self.closed.append(pin.assigned_name)
                return CLOSE_DONE

            def launch_action_bound(self, _action_id, pin):
                self.launched.append(pin.assigned_name)
                return LAUNCH_DONE

            def verify_attestation(self, _action_id, pin):
                self.verified.append(pin.assigned_name)
                if pin.assigned_name == plan.worker.assigned_name:
                    return worker_attestation
                return ATTEST_BOUND

        class FakeRoleOps:
            def __init__(self, *args, request=None, **kwargs):
                self.request = request

            def resume_lane_authority(self, _request):
                return True

            def lane_free_of_live_process(self, _request):
                # The worker's same-name slot is already live after launch succeeded,
                # while the gateway has not reached its launch effect yet.
                return self.request.assigned_name != plan.worker.assigned_name

            def replacement_store_admission(self, _key, _pin):
                return None

        fake_port = FakePort()
        fresh_plan = replace(
            plan,
            worker=replace(
                plan.worker,
                inventory_generation_matches=False,
                runtime_state=RUNTIME_UNKNOWN,
                attestation_readable=False,
            ),
        )
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(path=Path(temp) / "replacement.sqlite")
            key, participants = self._seed_worker_launch_owed(
                store, plan, request
            )
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store
            )
            with (
                mock.patch.object(ops, "observe", return_value=fresh_plan),
                mock.patch.object(
                    site, "LiveRecoveryActuatorPort", return_value=fake_port
                ),
                mock.patch.object(site, "LiveStaleWorkerRecoveryOps", FakeRoleOps),
            ):
                outcome = ops.replace_pair(request, plan)
            record = store.get(key)
            assert record is not None
            participant_phases = {
                pin.assigned_name: record.find_participant(pin.identity).phase
                for pin in participants
            }
            return outcome, fake_port, record.phase, participant_phases

    def test_launch_effect_crash_replay_adopts_exact_action_bound_live_slot(self) -> None:
        outcome, port, phase, participant_phases = (
            self._exercise_worker_launch_replay(ATTEST_BOUND)
        )

        self.assertTrue(outcome.completed, outcome.detail)
        self.assertEqual(phase, "completed")
        # Worker close and launch already happened before the simulated crash.  The
        # replay adopts it, re-verifies it, and never repeats either live effect.
        self.assertNotIn("managed-claude", port.closed)
        self.assertNotIn("managed-claude", port.launched)
        self.assertGreaterEqual(port.verified.count("managed-claude"), 2)
        self.assertEqual(participant_phases["managed-claude"], "replaced")
        # The not-yet-progressed sibling still follows the normal close/launch path.
        self.assertEqual(port.closed, ["managed-codex"])
        self.assertEqual(port.launched, ["managed-codex"])

    def test_launch_effect_crash_replay_refuses_foreign_live_slot(self) -> None:
        outcome, port, phase, participant_phases = (
            self._exercise_worker_launch_replay(ATTEST_MISMATCH)
        )

        self.assertFalse(outcome.completed)
        self.assertIn("launch_authority_moved", outcome.detail)
        self.assertEqual(phase, PHASE_REPLACING_NONSELF)
        self.assertEqual(
            participant_phases["managed-claude"], PARTICIPANT_LAUNCH_OWED
        )
        self.assertEqual(port.closed, [])
        self.assertEqual(port.launched, [])

    def test_live_composition_drives_both_exact_participants_to_completed(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        class FakePort:
            def __init__(self) -> None:
                self.closed: list[str] = []
                self.launched: list[str] = []
                self.verified: list[str] = []

            def observe_old_slot(self, _pin):
                return OLD_SLOT_PRESENT

            def observe_preservation(self, _pin):
                return PreservationObservation(
                    identity_matches=True, attestation_fresh=True
                )

            def close_exact_generation(self, pin):
                self.closed.append(pin.assigned_name)
                return CLOSE_DONE

            def launch_action_bound(self, _action_id, pin):
                self.launched.append(pin.assigned_name)
                return LAUNCH_DONE

            def verify_attestation(self, _action_id, pin):
                self.verified.append(pin.assigned_name)
                return ATTEST_BOUND

        class FakeRoleOps:
            def __init__(self, *args, **kwargs):
                pass

            def resume_lane_authority(self, _request):
                return True

            def lane_free_of_live_process(self, _request):
                return True

            def replacement_store_admission(self, _key, _pin):
                return None

        plan = _plan()
        request = _request(plan)
        fake_port = FakePort()
        with TemporaryDirectory() as temp:
            store = ReplacementTransactionStore(path=Path(temp) / "replacement.sqlite")
            ops = site.LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store
            )
            with (
                mock.patch.object(ops, "observe", return_value=plan),
                mock.patch.object(
                    site,
                    "plan_participants_with_evidence",
                    side_effect=lambda pins, **_kwargs: EvidencePlanning(
                        participants=tuple(pins)
                    ),
                ),
                mock.patch.object(
                    site, "LiveRecoveryActuatorPort", return_value=fake_port
                ),
                mock.patch.object(
                    site, "LiveStaleWorkerRecoveryOps", FakeRoleOps
                ),
            ):
                outcome = ops.replace_pair(request, plan)

            self.assertTrue(outcome.completed, outcome.detail)
            self.assertEqual(outcome.phase, "completed")
            self.assertCountEqual(
                fake_port.closed,
                [plan.gateway.assigned_name, plan.worker.assigned_name],
            )
            self.assertCountEqual(fake_port.launched, fake_port.closed)
            self.assertCountEqual(fake_port.verified, fake_port.closed)
            record = store.get(
                ReplacementTransactionKey(plan.workspace_id, plan.action_id)
            )
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.phase, "completed")
            self.assertEqual(record.lease_holder, "")


if __name__ == "__main__":
    unittest.main()
