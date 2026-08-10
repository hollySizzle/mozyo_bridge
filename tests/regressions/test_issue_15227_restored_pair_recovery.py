"""Regression coverage for post-reboot active-pair recovery (Redmine #15227)."""

from __future__ import annotations

import argparse
import unittest
from dataclasses import dataclass, replace
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_restored_pair_recovery_cli as cli_site,
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
    RESTORED_PAIR_RECOVERY_APPROVAL_GATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_recovery import (  # noqa: E501
    APPROVAL_DEGRADED,
    APPROVAL_HEALTHY,
    BLOCK_ATTESTATION_UNREADABLE,
    BLOCK_DEFAULT_LANE,
    BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,
    BLOCK_PAIR_HEALTHY,
    BLOCK_PAIR_INCOMPLETE,
    BLOCK_SLOT_BUSY,
    BLOCK_SLOT_RUNTIME_NOT_SETTLED,
    SLOT_GATEWAY,
    SLOT_WORKER,
    STATUS_COMPLETED,
    STATUS_REFUSED,
    RestoredPairPlan,
    RestoredSlot,
    restored_pair_approval_operation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
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


@dataclass(frozen=True)
class _ConditionalCloseCapablePlan(RestoredPairPlan):
    """Synthetic future adapter plan; never used by live production composition."""

    @property
    def generation_conditional_close_available(self) -> bool:
        return True


def _plan(
    *,
    gateway_cwd: bool = False,
    worker_cwd: bool = False,
    lane: str = "issue_15227_post_reboot_exact_relaunch",
    conditional_close_available: bool = True,
) -> RestoredPairPlan:
    plan_type = (
        _ConditionalCloseCapablePlan
        if conditional_close_available
        else RestoredPairPlan
    )
    return plan_type(
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
        self.progress_calls = 0
        self.approval_calls = 0
        self.replace_calls = 0

    def observe(self, _request):
        return self.plan

    def transaction_is_progressed_replay(self, _request, _plan) -> bool:
        self.progress_calls += 1
        return self.progressed_replay

    def approval_verified(self, _request, _plan) -> bool:
        self.approval_calls += 1
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
    def test_reboot_restored_pair_is_diagnostic_only_without_conditional_close(
        self,
    ) -> None:
        plan = _plan(
            gateway_cwd=False,
            worker_cwd=False,
            conditional_close_available=False,
        )
        self.assertFalse(plan.generation_conditional_close_available)
        self.assertFalse(plan.may_recover)
        self.assertEqual(
            plan.blocked_reasons,
            (BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,),
        )
        with self.assertRaises(TypeError):
            replace(plan, generation_conditional_close_available=True)

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
    def test_preflight_withholds_owner_marker_and_has_zero_effect(self) -> None:
        plan = _plan(conditional_close_available=False)
        ops = _Ops(plan)
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(_request(plan))
        self.assertFalse(outcome.executed)
        self.assertEqual(outcome.required_approval_marker, "")
        self.assertIn(
            BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,
            outcome.plan.blocked_reasons,
        )
        self.assertEqual(ops.progress_calls, 0)
        self.assertEqual(ops.approval_calls, 0)
        self.assertEqual(ops.replace_calls, 0)

    def test_programmatic_execute_stops_before_transaction_or_approval(self) -> None:
        plan = _plan(conditional_close_available=False)
        ops = _Ops(plan)
        ops.approved = True
        outcome = SublaneRestoredPairRecoveryUseCase(ops).run(
            _request(plan), execute=True
        )
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertIn(
            BLOCK_GENERATION_CONDITIONAL_CLOSE_UNAVAILABLE,
            outcome.plan.blocked_reasons,
        )
        self.assertEqual(ops.progress_calls, 0)
        self.assertEqual(ops.approval_calls, 0)
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
    def test_public_parser_is_read_only_and_keeps_diagnostic_pins(self) -> None:
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
            ]
        )
        self.assertEqual(args.gateway_approval_health, APPROVAL_DEGRADED)
        self.assertEqual(args.worker_approval_health, APPROVAL_HEALTHY)
        self.assertFalse(hasattr(args, "execute"))
        self.assertFalse(hasattr(args, "supersede"))
        for forbidden in ("--execute", "--supersede"):
            with self.subTest(forbidden=forbidden):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    build_parser().parse_args(
                        [
                            "sublane",
                            "recover-restored-pair",
                            "--issue",
                            "15227",
                            "--lane",
                            "issue_15227_post_reboot_exact_relaunch",
                            forbidden,
                        ]
                    )

    def test_cli_handler_ignores_programmatic_execute_attribute(self) -> None:
        plan = _plan(conditional_close_available=False)
        ops = _Ops(plan)
        args = argparse.Namespace(
            issue=plan.issue,
            lane=plan.lane,
            journal="",
            action_id="",
            action_generation=0,
            allow_pending_composer_loss=True,
            gateway_assigned_name="",
            gateway_locator="",
            gateway_revision="",
            worker_assigned_name="",
            worker_locator="",
            worker_revision="",
            gateway_approval_health="",
            worker_approval_health="",
            supersede=True,
            supersedes_generation=9,
            supersedes_journal="forged",
            supersedes_revision=12,
            execute=True,
            repo=None,
            json=False,
        )
        with (
            mock.patch.object(
                cli_site, "build_live_restored_pair_recovery_ops", return_value=ops
            ),
            mock.patch("sys.stdout"),
        ):
            code = cli_site.cmd_sublane_recover_restored_pair(args)
        self.assertEqual(code, 0)
        self.assertEqual(ops.progress_calls, 0)
        self.assertEqual(ops.approval_calls, 0)
        self.assertEqual(ops.replace_calls, 0)

    def test_preflight_text_and_json_expose_copyable_slot_health_pins(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_cli import (  # noqa: E501
            format_restored_pair_recovery_text,
        )

        plan = _plan(
            gateway_cwd=False,
            worker_cwd=True,
            conditional_close_available=False,
        )
        outcome = SublaneRestoredPairRecoveryUseCase(_Ops(plan)).run(
            _request(plan)
        )
        rendered = format_restored_pair_recovery_text(outcome)
        self.assertIn("generation_conditional_close_available: false", rendered)
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


class RestoredPairLiveEffectFenceTests(unittest.TestCase):
    def test_live_observe_ignores_supersede_without_opening_transaction_store(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_restored_pair_recovery_live as site,
        )

        plan = _plan()
        transaction_store = mock.Mock()
        ops = site.LiveRestoredPairRecoveryOps(
            repo_root=Path("."), transaction_store=transaction_store
        )
        request = replace(_request(plan), supersede=True)
        with mock.patch.object(
            site.LiveRestoredPairObservation, "observe", return_value=plan
        ):
            observed = ops.observe(request)

        self.assertFalse(observed.supersede_requested)
        transaction_store.get.assert_not_called()

    def test_live_replace_writes_no_transaction_and_calls_no_runner(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_recovery_live import (  # noqa: E501
            LiveRestoredPairRecoveryOps,
        )

        plan = _plan()
        request = _request(plan)
        runner = mock.Mock(side_effect=AssertionError("Herdr runner must not be called"))
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "replacement.sqlite"
            store = ReplacementTransactionStore(path=db_path)
            ops = LiveRestoredPairRecoveryOps(
                repo_root=Path(temp), transaction_store=store, runner=runner
            )

            outcome = ops.replace_pair(request, plan)

            self.assertFalse(outcome.completed)
            self.assertIn("generation-conditional close is unavailable", outcome.detail)
            runner.assert_not_called()
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()
