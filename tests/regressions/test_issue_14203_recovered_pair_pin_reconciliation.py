"""Regression coverage for recovered active-pair pin reconciliation (#14203 R19)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    BINDING_BOUND,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    CAS_ALREADY_DECLARED,
    CAS_STALE_REVISION,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.lane_recovered_pair_pin_reconcile import (
    LaneRecoveredPairPinReconcileStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovered_pair_pin_reconciliation_live import (  # noqa: E501
    LiveRecoveredPairPinReconciliationOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    hibernated_pair_recovery_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovered_pair_pin_reconciliation import (  # noqa: E501
    RecoveredPairPinReconciliationRequest,
    is_exact_reconciliation_authority,
    recovery_action_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wTest"
ISSUE = "14462"
LANE = "issue_14462_gateway_recovery_release_r1"
DECISION = "88145"
APPROVAL = "90001"
WORKTREE = "wt_test"
SOURCE_REVISION = 5
ACTIVE_REVISION = 1
GENERATION = 1


def _name(provider: str) -> str:
    return encode_assigned_name(WS, provider, LANE)


def _pins(gateway: str, worker: str) -> tuple[ProcessGenerationPin, ...]:
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=_name("codex"),
            locator=gateway,
            attested_at="2026-07-26T08:00:00+00:00",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=_name("claude"),
            locator=worker,
            attested_at="2026-07-26T08:00:00+00:00",
        ),
    )


OLD = _pins(f"{WS}:p14", f"{WS}:p15")
NEW = _pins(f"{WS}:p17", f"{WS}:p18")
ACTION = hibernated_pair_recovery_action_id(
    issue=ISSUE,
    lane_id=LANE,
    revision=str(SOURCE_REVISION),
    generation=str(GENERATION),
)


def _decision() -> DecisionPointer:
    return DecisionPointer(
        source="redmine", issue_id=ISSUE, journal_id=DECISION
    )


def _request(**overrides) -> RecoveredPairPinReconciliationRequest:
    values = {
        "issue": ISSUE,
        "lane": LANE,
        "journal": APPROVAL,
        "lifecycle_decision_journal": DECISION,
        "target_action_id": ACTION,
        "source_revision": SOURCE_REVISION,
        "expected_revision": ACTIVE_REVISION,
        "lane_generation": GENERATION,
        "worktree": "/unused",
    }
    values.update(overrides)
    return RecoveredPairPinReconciliationRequest(**values)


def _approval_note(request: RecoveredPairPinReconciliationRequest) -> str:
    return (
        "[mozyo:workflow-event:"
        "gate=owner_approval:"
        "kind=recovered_pair_pin_reconciliation:"
        f"issue={request.issue}:"
        f"lane={request.lane}:"
        f"lane_generation={request.lane_generation}:"
        f"source_revision={request.source_revision}:"
        f"expected_revision={request.expected_revision}:"
        f"lifecycle_decision_journal={request.lifecycle_decision_journal}:"
        f"target_action_digest={recovery_action_digest(request.target_action_id)}]"
    )


class RecoveredPairPinStoreTest(unittest.TestCase):
    def _declare(self, home: Path) -> None:
        result = LaneDeclarationStore(home=home).declare_lane(
            LaneLifecycleKey(WS, LANE),
            decision=_decision(),
            issue_id=ISSUE,
            worktree_identity=WORKTREE,
            declared_slots=OLD,
        )
        self.assertTrue(result.applied)

    def test_replaces_only_declared_slots_and_preserves_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._declare(home)
            result = LaneRecoveredPairPinReconcileStore(home=home).reconcile(
                LaneLifecycleKey(WS, LANE),
                expected_revision=1,
                expected_generation=1,
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                lifecycle_decision=_decision(),
                expected_old_slots=OLD,
                recovered_slots=NEW,
            )
            self.assertTrue(result.applied)
            self.assertEqual(result.revision, 2)
            record = LaneLifecycleStore(home=home).get(
                LaneLifecycleKey(WS, LANE)
            )
            pair = read_declared_pin_pair(record)
            self.assertTrue(pair.ok)
            self.assertEqual(pair.gateway.locator, f"{WS}:p17")
            self.assertEqual(pair.worker.locator, f"{WS}:p18")
            self.assertEqual(record.decision_journal, DECISION)
            self.assertEqual(record.lane_generation, 1)
            self.assertEqual(record.lane_disposition, "active")

    def test_stale_revision_and_divergent_old_pair_are_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._declare(home)
            store = LaneRecoveredPairPinReconcileStore(home=home)
            stale = store.reconcile(
                LaneLifecycleKey(WS, LANE),
                expected_revision=2,
                expected_generation=1,
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                lifecycle_decision=_decision(),
                expected_old_slots=OLD,
                recovered_slots=NEW,
            )
            self.assertFalse(stale.applied)
            self.assertEqual(stale.reason, CAS_STALE_REVISION)
            divergent = store.reconcile(
                LaneLifecycleKey(WS, LANE),
                expected_revision=1,
                expected_generation=1,
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                lifecycle_decision=_decision(),
                expected_old_slots=_pins(f"{WS}:p10", f"{WS}:p11"),
                recovered_slots=NEW,
            )
            self.assertFalse(divergent.applied)
            self.assertEqual(divergent.reason, CAS_ALREADY_DECLARED)
            self.assertEqual(
                LaneLifecycleStore(home=home)
                .get(LaneLifecycleKey(WS, LANE))
                .revision,
                1,
            )

    def test_byte_equal_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._declare(home)
            store = LaneRecoveredPairPinReconcileStore(home=home)
            first = store.reconcile(
                LaneLifecycleKey(WS, LANE),
                expected_revision=1,
                expected_generation=1,
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                lifecycle_decision=_decision(),
                expected_old_slots=OLD,
                recovered_slots=NEW,
            )
            replay = store.reconcile(
                LaneLifecycleKey(WS, LANE),
                expected_revision=1,
                expected_generation=1,
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                lifecycle_decision=_decision(),
                expected_old_slots=OLD,
                recovered_slots=NEW,
            )
            self.assertTrue(replay.applied)
            self.assertEqual(replay.revision, first.revision)


class ReconciliationAuthorityTest(unittest.TestCase):
    def test_exact_structured_authority_only(self) -> None:
        request = _request()
        entry = RedmineJournalEntry(
            issue_id=ISSUE,
            journal_id=APPROVAL,
            notes=_approval_note(request),
        )
        self.assertTrue(is_exact_reconciliation_authority(entry, request))
        self.assertFalse(
            is_exact_reconciliation_authority(
                RedmineJournalEntry(
                    issue_id=ISSUE,
                    journal_id=APPROVAL,
                    notes=_approval_note(
                        _request(expected_revision=ACTIVE_REVISION + 1)
                    ),
                ),
                request,
            )
        )
        duplicate = RedmineJournalEntry(
            issue_id=ISSUE,
            journal_id=APPROVAL,
            notes=_approval_note(request) + _approval_note(request),
        )
        self.assertFalse(is_exact_reconciliation_authority(duplicate, request))

    def test_rejects_noncanonical_authority_field_grammar(self) -> None:
        request = _request()
        exact = _approval_note(request)
        variants = (
            exact.replace(
                "expected_revision=1",
                "expected_revision=999:expected_revision=1",
            ),
            exact.replace("]", ":unexpected=accepted]"),
            exact.replace("]", ":badtoken]"),
            exact.replace(f":lane={LANE}", ""),
            exact.replace(f"lane={LANE}", "lane="),
        )
        for notes in variants:
            with self.subTest(notes=notes):
                self.assertFalse(
                    is_exact_reconciliation_authority(
                        RedmineJournalEntry(
                            issue_id=ISSUE,
                            journal_id=APPROVAL,
                            notes=notes,
                        ),
                        request,
                    )
                )


class _FakeLiveOps(LiveRecoveredPairPinReconciliationOps):
    def __init__(self, home: Path):
        super().__init__(
            repo_root=Path("/unused"),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        self.request = _request()

    def _worktree(self, request):
        return Path("/unused"), WS, WORKTREE

    def _entries(self, issue):
        return (
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id=APPROVAL,
                notes=_approval_note(self.request),
            ),
        )

    @staticmethod
    def _providers(root):
        return ("codex", "claude")

    def _rows(self):
        return (
            {
                "name": _name("codex"),
                "pane_id": f"{WS}:p17",
                "provider": "codex",
                "agent": "codex",
                "revision": "r17",
            },
            {
                "name": _name("claude"),
                "pane_id": f"{WS}:p18",
                "provider": "claude",
                "agent": "claude",
                "revision": "r18",
            },
        )

    def _read_attestation(self, assigned_name):
        provider = "codex" if assigned_name == _name("codex") else "claude"
        locator = f"{WS}:p17" if provider == "codex" else f"{WS}:p18"
        return IdentityAttestationRecord(
            assigned_name=assigned_name,
            workspace_id=WS,
            role=provider,
            lane_id=LANE,
            locator=locator,
            verdict=VERDICT_PRESENT,
            observed_at="2026-07-26T09:00:00+00:00",
        )

    def _target_ready(self, root, request):
        return SimpleNamespace(may_deliver=True, detail="ready")

    def _attestation_is_v1(self, home):
        return True

    def _replacement_binding(self, home, action_id, assigned_name):
        if assigned_name == _name("codex"):
            old, new = f"{WS}:p14", f"{WS}:p17"
        else:
            old, new = f"{WS}:p15", f"{WS}:p18"
        return SimpleNamespace(
            phase=BINDING_BOUND, old_locator=old, locator=new
        )


class LiveReconciliationTest(unittest.TestCase):
    def test_live_join_then_cas_updates_exact_active_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneDeclarationStore(home=home).declare_lane(
                LaneLifecycleKey(WS, LANE),
                decision=_decision(),
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                declared_slots=OLD,
            )
            ops = _FakeLiveOps(home)
            preflight = ops.preflight(ops.request)
            self.assertTrue(preflight.ready, preflight.detail)
            applied, revision, detail = ops.reconcile(ops.request)
            self.assertTrue(applied, detail)
            self.assertEqual(revision, 2)
            replay_preflight = ops.preflight(ops.request)
            self.assertTrue(replay_preflight.ready, replay_preflight.detail)
            replayed, replay_revision, replay_detail = ops.reconcile(ops.request)
            self.assertTrue(replayed, replay_detail)
            self.assertEqual(replay_revision, 2)

    def test_wrong_recovery_action_fails_before_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneDeclarationStore(home=home).declare_lane(
                LaneLifecycleKey(WS, LANE),
                decision=_decision(),
                issue_id=ISSUE,
                worktree_identity=WORKTREE,
                declared_slots=OLD,
            )
            ops = _FakeLiveOps(home)
            request = _request(target_action_id="wrong")
            self.assertEqual(
                ops.preflight(request).detail, "recovery_action_mismatch"
            )
            self.assertEqual(
                LaneLifecycleStore(home=home)
                .get(LaneLifecycleKey(WS, LANE))
                .revision,
                1,
            )


if __name__ == "__main__":
    unittest.main()
