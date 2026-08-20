"""Regression pin: a supersede-minted recovery lane regains its kind (Redmine #15774).

The measured incident (2026-08-20): L2 lane ``issue_15693_l2_trial`` was superseded to
its recovery lane after a Herdr server loss. ``supersede_and_activate`` deliberately
mints the recovery row with an EMPTY ``lane_kind`` ("its own create declares a kind"),
but the create path could not: ``declare_lane`` saw the existing row as a divergent
re-declare and refused, and ``backfill_active_binding`` filled only the worktree binding
and pins — never the kind. The recovery L2 therefore stayed kind-less and every
delegated child create was refused ``sender_lane_not_delegated_coordinator``
(#15693 j#108814, #15745 j#108813), stalling all L3 dispatch.

This file pins the recurrence end-to-end at the adopt-declaration seam: a
supersede-minted row + a live attested pair + the creating caller's ``--lane-kind``
assertion must land the kind on the row (``ADOPT_DECL_BACKFILLED``), and a divergent
non-empty assertion must stay a zero-write refusal.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402,E501
    ADOPT_DECL_ALREADY_OWNED,
    ADOPT_DECL_BACKFILLED,
    declare_adopted_owner_row,
)

WS = "ws-15774"
ORIGINAL = "issue_15693_l2_trial_like"
RECOVERY = "issue_15693_l2_r2_like"
ISSUE = "15693"
JOURNAL = "108814"
KIND = "delegated_coordinator"
PROVIDERS = ("codex", "claude")
GW_LOC = "w1V:pB"
WK_LOC = "w1V:pC"
ATTESTED_AT = "2026-08-20T08:41:00+00:00"


def _row(provider: str, locator: str) -> dict:
    return {
        "name": encode_assigned_name(WS, provider, RECOVERY),
        "pane_id": locator,
        "terminal_id": f"terminal:{locator}",
    }


class SupersededRecoveryLaneKindTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_recovery")
        self.decision = DecisionPointer(
            source="redmine", issue_id=ISSUE, journal_id=JOURNAL
        )
        self.declaration = LaneDeclarationStore(home=self.home)
        self.lifecycle = LaneLifecycleStore(home=self.home)
        # The incident shape: an original kind-bearing owner superseded to a
        # recovery lane whose row is deliberately kind-less.
        declared = self.declaration.declare_lane(
            LaneLifecycleKey(WS, ORIGINAL),
            decision=self.decision,
            binding_kind="issue",
            issue_id=ISSUE,
            worktree_identity="wt_original_token",
            lane_kind=KIND,
        )
        self.assertTrue(declared.applied)
        outcome = self.lifecycle.supersede_and_activate(
            superseded=LaneLifecycleKey(WS, ORIGINAL),
            expected_revision=declared.revision,
            recovery=LaneLifecycleKey(WS, RECOVERY),
            decision=self.decision,
        )
        self.assertTrue(outcome.applied)
        for provider, locator in zip(PROVIDERS, (GW_LOC, WK_LOC)):
            HerdrIdentityAttestationStore(home=self.home).upsert(
                IdentityAttestationRecord(
                    assigned_name=encode_assigned_name(WS, provider, RECOVERY),
                    workspace_id=WS,
                    role=provider,
                    lane_id=RECOVERY,
                    locator=locator,
                    terminal_id=f"terminal:{locator}",
                    verdict=VERDICT_PRESENT,
                    observed_at=ATTESTED_AT,
                )
            )

    def _adopt(self, lane_kind: str) -> str:
        return declare_adopted_owner_row(
            journal=JOURNAL,
            issue=ISSUE,
            lane_label=RECOVERY,
            worktree_path=self.worktree,
            workspace_id=WS,
            lane_id=RECOVERY,
            providers=PROVIDERS,
            rows=[_row("codex", GW_LOC), _row("claude", WK_LOC)],
            lane_kind=lane_kind,
            store_factory=lambda: LaneDeclarationStore(home=self.home),
            attestation_store_factory=lambda: HerdrIdentityAttestationStore(
                home=self.home
            ),
        )

    def test_the_callers_kind_assertion_lands_on_the_supersede_minted_row(self) -> None:
        self.assertEqual(
            self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY)).lane_kind, ""
        )
        self.assertEqual(self._adopt(KIND), ADOPT_DECL_BACKFILLED)
        row = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(row.lane_kind, KIND)

    def test_a_divergent_kind_assertion_is_zero_write(self) -> None:
        self.assertEqual(self._adopt(KIND), ADOPT_DECL_BACKFILLED)
        # The backfill CAS refuses the divergent kind; the adopt then reads the lane
        # as an established owner with a complete binding (`already_owned`). The pin
        # that matters is the STATE invariant: the stored kind is never edited.
        self.assertEqual(self._adopt("implementation"), ADOPT_DECL_ALREADY_OWNED)
        row = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(row.lane_kind, KIND)

    def test_no_assertion_backfills_the_binding_but_never_guesses_a_kind(self) -> None:
        self.assertEqual(self._adopt(""), ADOPT_DECL_BACKFILLED)
        row = self.lifecycle.get(LaneLifecycleKey(WS, RECOVERY))
        self.assertEqual(row.lane_kind, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
