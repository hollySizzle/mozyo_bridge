"""Regression for Redmine #14955: minted epoch replaces legacy generation fences."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E402,E501
    ResumeRequest,
    SublaneResumeUseCase,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

WS = "w14955"
LANE = "issue_14955_epoch_precedence"
ISSUE = "14955"


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="98896")


def _name(role: str) -> str:
    return encode_assigned_name(WS, role, LANE)


class _Ops:
    def __init__(self, *, gateway_epoch: str = "1", worker_epoch: str = "1") -> None:
        self._rows = [
            {
                "name": _name("codex"),
                "pane_id": f"{WS}:p20",
                "terminal_id": f"terminal:{WS}:p20",
            },
            {
                "name": _name("claude"),
                "pane_id": f"{WS}:p21",
                "terminal_id": f"terminal:{WS}:p21",
            },
        ]
        self._attestations = {
            _name("codex"): self._record("codex", "p20", gateway_epoch),
            _name("claude"): self._record("claude", "p21", worker_epoch),
        }

    @staticmethod
    def _record(role: str, pane: str, epoch: str) -> IdentityAttestationRecord:
        return IdentityAttestationRecord(
            assigned_name=_name(role),
            workspace_id=WS,
            role=role,
            lane_id=LANE,
            locator=f"{WS}:{pane}",
            terminal_id=f"terminal:{WS}:{pane}",
            verdict=VERDICT_PRESENT,
            # Deliberately older than the hibernate transition. A minted exact epoch, not
            # this caller-controlled clock, proves which generation the process belongs to.
            observed_at="2026-01-01T00:00:00+00:00",
            lane_epoch=epoch,
        )

    def workspace_id(self) -> str:
        return WS

    def live_rows(self):
        return list(self._rows)

    def read_attestation(self, assigned_name: str):
        return self._attestations.get(assigned_name)

    def provider_pair(self):
        return ("codex", "claude")


def _hibernated_store(tmp: str) -> tuple[LaneLifecycleStore, LaneLifecycleKey]:
    store = LaneLifecycleStore(home=Path(tmp))
    key = LaneLifecycleKey(WS, LANE)
    store.declare_active(
        key,
        decision=_decision(),
        issue_id=ISSUE,
        now="2026-02-01T00:00:00+00:00",
    )
    store.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=1,
        target=DISPOSITION_HIBERNATED,
        decision=_decision(),
        now="2026-02-02T00:00:00+00:00",
    )
    return store, key


def _run(store: LaneLifecycleStore, ops: _Ops, *, execute: bool = True):
    return SublaneResumeUseCase(ops=ops, store=store).run(
        ResumeRequest(issue=ISSUE, lane=LANE, journal="98896"),
        execute=execute,
    )


class MintedEpochAuthorityPrecedenceTest(unittest.TestCase):
    def test_exact_epoch_resumes_without_release_evidence_or_freshness_anchor(self) -> None:
        """The exact live #13842/#14755 post-adoption shape."""
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_store(tmp)
            # Offline legacy adoption mints the epoch on a row that predates hibernated_at;
            # no release observation exists. Keep those facts byte-exact.
            with sqlite3.connect(store.path) as conn:
                conn.execute(
                    "UPDATE lane_lifecycle_records SET hibernated_at = '' "
                    "WHERE repo_workspace_id = ? AND lane_id = ?",
                    (WS, LANE),
                )
            outcome = _run(store, _Ops())
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)
            self.assertNotIn("release_evidence_absent", outcome.preflight.pair_attestation_detail)
            self.assertNotIn("freshness anchor", outcome.preflight.pair_attestation_detail)
            self.assertEqual(store.get(key).lane_disposition, DISPOSITION_ACTIVE)

    def test_future_slot_epoch_refuses_even_if_legacy_clock_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_store(tmp)
            outcome = _run(store, _Ops(gateway_epoch="2"))
            self.assertTrue(outcome.is_blocked)
            self.assertIn("lane_epoch_malformed", outcome.preflight.pair_attestation_detail)
            self.assertIsNone(outcome.transition)
            self.assertEqual(store.get(key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_one_slot_without_current_epoch_refuses_the_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_store(tmp)
            outcome = _run(store, _Ops(worker_epoch=""))
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                "claude: lane_epoch_attestation_absent",
                outcome.preflight.pair_attestation_detail,
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(store.get(key).lane_disposition, DISPOSITION_HIBERNATED)

    def test_unminted_epoch_cannot_promote_legacy_evidence_to_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, key = _hibernated_store(tmp)
            # A pre-v10 projection is unminted even if it happens to carry a fresh clock.
            with sqlite3.connect(store.path) as conn:
                conn.execute(
                    "UPDATE lane_lifecycle_records SET lane_epoch = ? "
                    "WHERE repo_workspace_id = ? AND lane_id = ?",
                    ("0", WS, LANE),
                )
            outcome = _run(store, _Ops())
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                "lane_epoch_authority_unavailable",
                outcome.preflight.pair_attestation_detail,
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(store.get(key).lane_disposition, DISPOSITION_HIBERNATED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
