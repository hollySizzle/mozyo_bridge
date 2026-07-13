"""`sublane supersede` use case tests (Redmine #13681 W2).

Drives :class:`SublaneSupersedeUseCase` over a fake IO port (fake live herdr
inventory + fake attestation reads + captured guarded close) and a real
:class:`LaneLifecycleStore` over a temp home. Covers the fail-closed preflight, the
atomic ownership handover commit point, the tombstone-free process release, and the
idempotent partial-release resume.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_SUPERSEDED,
    OWNER_RESOLVED,
    RELEASE_PARTIAL,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_supersede import (  # noqa: E501
    BLOCK_ORIGINAL_NOT_IDLE,
    BLOCK_RECOVERY_ATTESTATION,
    SublaneSupersedeUseCase,
    SupersedeAssertions,
    SupersedeRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "13583"
ORIG = "issue_13583_x"
REC = "issue_13583_recovery"
JOURNAL = "76630"


def _row(role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(WS, role, lane), "pane_id": locator}


def _attest(role: str, lane: str, locator: str, verdict: str = VERDICT_PRESENT):
    return IdentityAttestationRecord(
        assigned_name=encode_assigned_name(WS, role, lane),
        workspace_id=WS,
        role=role,
        lane_id=lane,
        locator=locator,
        verdict=verdict,
    )


class _FakeOps:
    """Fake supersede IO port: canned workspace / rows / attestations / close."""

    def __init__(self, *, rows, attestations, close_result=None):
        self._rows = list(rows)
        self._attest = dict(attestations)
        self._close_result = close_result
        self.close_calls: list = []

    def workspace_id(self) -> str:
        return WS

    def live_rows(self):
        return list(self._rows)

    def read_attestation(self, assigned_name):
        return self._attest.get(assigned_name)

    def execute_close(self, plan):
        self.close_calls.append(plan)
        if self._close_result is not None:
            return self._close_result
        return HerdrRetireCloseResult(
            workspace_id=plan.workspace_id,
            lane_id=plan.lane_id,
            closed=tuple(plan.close_targets),
            failed=(),
            foreign_names=plan.foreign_names,
        )

    def drop_rows_for(self, lane: str) -> None:
        """Simulate the original's slots being gone (a prior close succeeded)."""
        self._rows = [r for r in self._rows if f"_{lane}" not in r["name"]]


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)


def _request(**kw) -> SupersedeRequest:
    assertions = kw.pop(
        "assertions",
        SupersedeAssertions(
            callbacks_drained=True, no_pending_prompt=True, not_working=True
        ),
    )
    return SupersedeRequest(
        issue=kw.get("issue", ISSUE),
        original_lane=kw.get("original_lane", ORIG),
        recovery_lane=kw.get("recovery_lane", REC),
        journal=kw.get("journal", JOURNAL),
        assertions=assertions,
    )


class SublaneSupersedeTest(unittest.TestCase):
    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _declare_original(self, store) -> None:
        store.declare_active(
            LaneLifecycleKey(WS, ORIG), decision=_decision(), issue_id=ISSUE
        )

    def _both_lanes_live_ops(self, **kw) -> _FakeOps:
        rows = [
            _row("codex", ORIG, f"{WS}:p2"),
            _row("claude", ORIG, f"{WS}:p3"),
            _row("codex", REC, f"{WS}:p10"),
            _row("claude", REC, f"{WS}:p11"),
        ]
        attest = {
            encode_assigned_name(WS, "codex", REC): _attest("codex", REC, f"{WS}:p10"),
            encode_assigned_name(WS, "claude", REC): _attest("claude", REC, f"{WS}:p11"),
        }
        return _FakeOps(rows=rows, attestations=attest, **kw)

    def test_happy_path_hands_ownership_over_and_releases_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            ops = self._both_lanes_live_ops()
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            outcome = use_case.run(_request(), execute=True)

            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.supersede.applied)
            # Original went active -> superseded; recovery is now the single owner.
            original = store.get(LaneLifecycleKey(WS, ORIG))
            recovery = store.get(LaneLifecycleKey(WS, REC))
            self.assertEqual(original.lane_disposition, DISPOSITION_SUPERSEDED)
            self.assertEqual(recovery.lane_disposition, DISPOSITION_ACTIVE)
            owner = store.resolve_owner(WS, ISSUE)
            self.assertEqual(owner.status, OWNER_RESOLVED)
            self.assertEqual(owner.lane_id, REC)
            # The original's two managed slots were closed; the release is released.
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertEqual(len(outcome.release.closed), 2)
            self.assertEqual(store.get(LaneLifecycleKey(WS, ORIG)).process_release,
                             RELEASE_RELEASED)
            # The close only ever targeted the ORIGINAL lane's slots — never recovery's.
            closed_locators = {loc for _, loc in outcome.release.closed}
            self.assertEqual(closed_locators, {f"{WS}:p2", f"{WS}:p3"})

    def test_blocks_when_recovery_not_attested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            # Recovery slots live, but the gateway self-attestation is missing.
            rows = [
                _row("codex", ORIG, f"{WS}:p2"),
                _row("claude", ORIG, f"{WS}:p3"),
                _row("codex", REC, f"{WS}:p10"),
                _row("claude", REC, f"{WS}:p11"),
            ]
            attest = {
                encode_assigned_name(WS, "codex", REC): _attest(
                    "codex", REC, f"{WS}:p10", verdict=VERDICT_MISSING
                ),
                encode_assigned_name(WS, "claude", REC): _attest(
                    "claude", REC, f"{WS}:p11"
                ),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            outcome = use_case.run(_request(), execute=True)

            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_RECOVERY_ATTESTATION, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.supersede)
            # Nothing mutated — the original still actively owns the issue.
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, ORIG)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(store.resolve_owner(WS, ISSUE).lane_id, ORIG)
            self.assertEqual(ops.close_calls, [])

    def test_blocks_when_original_not_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            ops = self._both_lanes_live_ops()
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            outcome = use_case.run(
                _request(
                    assertions=SupersedeAssertions(
                        callbacks_drained=True, no_pending_prompt=False, not_working=True
                    )
                ),
                execute=True,
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_ORIGINAL_NOT_IDLE, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.supersede)
            self.assertEqual(store.resolve_owner(WS, ISSUE).lane_id, ORIG)

    def test_preflight_only_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            ops = self._both_lanes_live_ops()
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            outcome = use_case.run(_request(), execute=False)

            self.assertTrue(outcome.preflight.may_supersede)
            self.assertFalse(outcome.executed)
            self.assertFalse(outcome.is_blocked)
            self.assertIsNone(outcome.supersede)
            # No mutation, no close.
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, ORIG)).lane_disposition,
                DISPOSITION_ACTIVE,
            )
            self.assertEqual(ops.close_calls, [])

    def test_partial_release_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            # First run: the gateway close fails -> partial (worker closed, gateway not).
            partial = HerdrRetireCloseResult(
                workspace_id=WS,
                lane_id=ORIG,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            ops = self._both_lanes_live_ops(close_result=partial)
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            first = use_case.run(_request(), execute=True)
            self.assertTrue(first.supersede.applied)
            self.assertEqual(first.release.process_release, RELEASE_PARTIAL)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, ORIG)).process_release, RELEASE_PARTIAL
            )
            action_first = store.get(LaneLifecycleKey(WS, ORIG)).release_action_id
            self.assertTrue(action_first)

            # Second run: ownership already handed over. Resume the SAME generation and
            # this time the remaining slot closes -> released. No new generation opened.
            ops2 = self._both_lanes_live_ops()  # default close: all targets succeed
            resume = SublaneSupersedeUseCase(ops=ops2, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(resume.already_handed_over)
            self.assertFalse(resume.is_blocked)
            self.assertEqual(resume.release.process_release, RELEASE_RELEASED)
            final = store.get(LaneLifecycleKey(WS, ORIG))
            self.assertEqual(final.process_release, RELEASE_RELEASED)
            # Same action generation across the resume (never opened a second one).
            self.assertEqual(resume.release.action_id, action_first)

    def test_crash_after_commit_before_release_resumes(self) -> None:
        # A crash between the supersede commit and the release: the store has the
        # handover but process_release is still not_requested. A re-run detects the
        # handover, opens the generation, closes the slots -> released.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            # Simulate only the commit landing (the actuator died before _drive_release).
            store.supersede_and_activate(
                superseded=LaneLifecycleKey(WS, ORIG),
                expected_revision=1,
                recovery=LaneLifecycleKey(WS, REC),
                decision=_decision(),
            )
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, ORIG)).process_release, "not_requested"
            )
            ops = self._both_lanes_live_ops()
            outcome = SublaneSupersedeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.already_handed_over)
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertEqual(len(ops.close_calls), 1)

    def test_partial_pair_releases_the_single_live_slot(self) -> None:
        # The original already lost its gateway; only the worker is live. The release
        # closes the one slot and records released (every pinned slot closed).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            rows = [
                _row("claude", ORIG, f"{WS}:p3"),  # only the worker of the original
                _row("codex", REC, f"{WS}:p10"),
                _row("claude", REC, f"{WS}:p11"),
            ]
            attest = {
                encode_assigned_name(WS, "codex", REC): _attest("codex", REC, f"{WS}:p10"),
                encode_assigned_name(WS, "claude", REC): _attest("claude", REC, f"{WS}:p11"),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneSupersedeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.supersede.applied)
            self.assertEqual(outcome.release.process_release, RELEASE_RELEASED)
            self.assertEqual({loc for _, loc in outcome.release.closed}, {f"{WS}:p3"})

    def test_original_with_dead_processes_supersedes_with_no_release(self) -> None:
        # The original's slots are already gone. Ownership still hands over; there is
        # nothing to release (a superseded lane draws zero capacity regardless).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            rows = [
                _row("codex", REC, f"{WS}:p10"),
                _row("claude", REC, f"{WS}:p11"),
            ]
            attest = {
                encode_assigned_name(WS, "codex", REC): _attest("codex", REC, f"{WS}:p10"),
                encode_assigned_name(WS, "claude", REC): _attest("claude", REC, f"{WS}:p11"),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneSupersedeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.supersede.applied)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, ORIG)).lane_disposition,
                DISPOSITION_SUPERSEDED,
            )
            self.assertEqual(outcome.release.process_release, "not_requested")
            self.assertEqual(ops.close_calls, [])

    def test_resume_never_closes_a_recycled_replacement_pane(self) -> None:
        # R1 F1 (j#77247): a partial release stays open pinned to the ORIGINAL locators.
        # If the slots are recycled into new agent generations (same assigned name, NEW
        # locator) before the resume, the resume must close NOTHING — a stale release
        # never kills a replacement pane.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            partial = HerdrRetireCloseResult(
                workspace_id=WS,
                lane_id=ORIG,
                closed=(("claude", f"{WS}:p3"),),
                failed=(("codex", f"{WS}:p2", "close_failed"),),
            )
            first = SublaneSupersedeUseCase(
                ops=self._both_lanes_live_ops(close_result=partial), store=store
            ).run(_request(), execute=True)
            self.assertEqual(first.release.process_release, RELEASE_PARTIAL)

            # The original's slots are recycled: same assigned names, NEW locators.
            recycled = [
                _row("codex", ORIG, f"{WS}:pNEWc"),
                _row("claude", ORIG, f"{WS}:pNEWw"),
                _row("codex", REC, f"{WS}:p10"),
                _row("claude", REC, f"{WS}:p11"),
            ]
            attest = {
                encode_assigned_name(WS, "codex", REC): _attest("codex", REC, f"{WS}:p10"),
                encode_assigned_name(WS, "claude", REC): _attest("claude", REC, f"{WS}:p11"),
            }
            ops2 = _FakeOps(rows=recycled, attestations=attest)
            resume = SublaneSupersedeUseCase(ops=ops2, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(resume.already_handed_over)
            # Nothing was closed: the pinned locators no longer match the live ones.
            all_targets = [
                t for call in ops2.close_calls for t in call.close_targets
            ]
            self.assertEqual(all_targets, [])
            closed_locators = {
                loc for call in ops2.close_calls for _, loc in call.close_targets
            }
            self.assertNotIn(f"{WS}:pNEWc", closed_locators)
            self.assertNotIn(f"{WS}:pNEWw", closed_locators)

    def test_incomplete_identity_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._declare_original(store)
            ops = self._both_lanes_live_ops()
            use_case = SublaneSupersedeUseCase(ops=ops, store=store)
            # A non-decimal journal cannot anchor a decision -> fail closed, no mutation.
            outcome = use_case.run(_request(journal="not-a-number"), execute=True)
            self.assertTrue(outcome.is_blocked)
            self.assertIsNone(outcome.supersede)
            self.assertEqual(store.resolve_owner(WS, ISSUE).lane_id, ORIG)


if __name__ == "__main__":
    unittest.main()
