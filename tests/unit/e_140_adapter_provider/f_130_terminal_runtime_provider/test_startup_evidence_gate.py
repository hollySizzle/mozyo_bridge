"""The post-launch startup-evidence gate (Redmine #14231 Acceptance 3, fail-closed half).

j#84724 / j#84743 require that a launch whose own execution-stage evidence is missing does
NOT report green — even when the slot itself looks live — and that the gap is never read as
"the wrapper never ran" or "the provider exited". These tests pin three things:

1. the gate only ever downgrades a would-be ``healthy``; it never masks a more specific,
   actionable verdict above it in the precedence order;
2. the default (:data:`EVIDENCE_NOT_APPLICABLE`) leaves every pre-#14231 verdict
   byte-invariant, so an unwrapped launch / an older launcher / a caller that does not
   consult the projection is unaffected;
3. the verdict owes NO rollback compensation. The slot was observed live, screen-clear and
   attested; only the record of how it got there is missing. Demanding a close there would
   destroy a working pane over a reporting gap AND would give the projection rollback
   authority by its absence — which j#84724 explicitly forbids.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E402,E501
    _slot_health,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E402,E501
    ATTESTATION_ABSENT,
    ATTESTATION_INVALID,
    ATTESTATION_OK,
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    DISPOSITION_FRESH_LAUNCHED,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_PRESENT,
    EVIDENCE_UNAVAILABLE,
    HEALTH_ATTESTATION_MISMATCH,
    HEALTH_ATTESTATION_TIMEOUT,
    HEALTH_DETAIL,
    HEALTH_HEALTHY,
    HEALTH_INVENTORY_UNREADABLE,
    HEALTH_LOCATOR_DRIFT,
    HEALTH_OUTCOMES,
    HEALTH_PROVIDER_EXITED,
    HEALTH_SHELL_RESIDUE,
    HEALTH_STARTUP_EVIDENCE_UNAVAILABLE,
    HEALTH_STARTUP_INTERACTION,
    SCREEN_BLOCKED,
    SCREEN_CLEAR,
    STARTUP_EVIDENCE_STATES,
    classify_startup_health,
)


def _facts(**over):
    """The all-positive (healthy) fact set; each test negates exactly one thing."""
    base = dict(
        inventory_readable=True,
        row_present=True,
        row_stale=False,
        live_locator="w2G:p3",
        launched_locator="w2G:p3",
        screen=SCREEN_CLEAR,
        attestation=ATTESTATION_OK,
    )
    base.update(over)
    return base


class EvidenceGateVocabularyTest(unittest.TestCase):
    def test_evidence_states_are_a_closed_tri_state(self):
        self.assertEqual(
            STARTUP_EVIDENCE_STATES,
            {EVIDENCE_NOT_APPLICABLE, EVIDENCE_PRESENT, EVIDENCE_UNAVAILABLE},
        )

    def test_new_verdict_is_in_the_closed_outcome_set_and_has_a_detail(self):
        self.assertIn(HEALTH_STARTUP_EVIDENCE_UNAVAILABLE, HEALTH_OUTCOMES)
        detail = HEALTH_DETAIL[HEALTH_STARTUP_EVIDENCE_UNAVAILABLE]
        # The detail must point at the public read surface and must NOT overclaim.
        self.assertIn("startup-status", detail)
        self.assertIn("not proof", detail)


class EvidenceGateClassificationTest(unittest.TestCase):
    def test_default_is_byte_invariant_with_pre_14231(self):
        # No evidence argument at all == the pre-#14231 call shape.
        self.assertEqual(classify_startup_health(**_facts()), HEALTH_HEALTHY)
        self.assertEqual(
            classify_startup_health(**_facts(), evidence=EVIDENCE_NOT_APPLICABLE),
            HEALTH_HEALTHY,
        )

    def test_evidence_present_stays_healthy(self):
        self.assertEqual(
            classify_startup_health(**_facts(), evidence=EVIDENCE_PRESENT),
            HEALTH_HEALTHY,
        )

    def test_evidence_unavailable_downgrades_the_would_be_green(self):
        self.assertEqual(
            classify_startup_health(**_facts(), evidence=EVIDENCE_UNAVAILABLE),
            HEALTH_STARTUP_EVIDENCE_UNAVAILABLE,
        )

    def test_evidence_gap_never_masks_a_more_specific_verdict(self):
        # Every named cause above the gate in the precedence order must survive an
        # unavailable-evidence reading: the operator needs the actionable cause, not a
        # meta-observation about our own bookkeeping.
        for negation, expected in (
            (dict(inventory_readable=False), HEALTH_INVENTORY_UNREADABLE),
            (dict(row_present=False), HEALTH_PROVIDER_EXITED),
            (dict(row_stale=True), HEALTH_SHELL_RESIDUE),
            (dict(live_locator="w2G:p9"), HEALTH_LOCATOR_DRIFT),
            (dict(screen=SCREEN_BLOCKED), HEALTH_STARTUP_INTERACTION),
            (dict(attestation=ATTESTATION_ABSENT), HEALTH_ATTESTATION_TIMEOUT),
            (dict(attestation=ATTESTATION_INVALID), HEALTH_ATTESTATION_MISMATCH),
        ):
            with self.subTest(negation=sorted(negation)):
                self.assertEqual(
                    classify_startup_health(
                        **_facts(**negation), evidence=EVIDENCE_UNAVAILABLE
                    ),
                    expected,
                )

    def test_gate_is_total_over_the_closed_evidence_vocabulary(self):
        for state in sorted(STARTUP_EVIDENCE_STATES):
            with self.subTest(evidence=state):
                verdict = classify_startup_health(**_facts(), evidence=state)
                self.assertIn(verdict, HEALTH_OUTCOMES)


class EvidenceGateCompensationTest(unittest.TestCase):
    """The gate is fail-closed for REPORTING; it never demands a destructive close."""

    def _slot(self, health):
        return _slot_health(
            slot_provider="claude",
            assigned_name="mzb1_ws1_claude_default",
            locator="w2G:p3",
            disposition=DISPOSITION_FRESH_LAUNCHED,
            health=health,
            blocker_id="",
        )

    def test_evidence_unavailable_owes_no_rollback(self):
        # The slot was observed live / screen-clear / attested. Closing it because our own
        # evidence write is missing would destroy working work over a reporting gap, and
        # would hand the projection rollback authority by its absence (j#84724 forbids it).
        slot = self._slot(HEALTH_STARTUP_EVIDENCE_UNAVAILABLE)
        self.assertEqual(slot.compensation, COMPENSATION_NOT_NEEDED)
        self.assertNotEqual(slot.health, HEALTH_HEALTHY)  # still not a green

    def test_other_non_green_verdicts_still_owe_a_rollback(self):
        # The carve-out is exactly one verdict wide -- every other failure keeps the
        # #13948 compensation contract unchanged.
        for health in (
            HEALTH_PROVIDER_EXITED,
            HEALTH_SHELL_RESIDUE,
            HEALTH_LOCATOR_DRIFT,
            HEALTH_ATTESTATION_TIMEOUT,
            HEALTH_INVENTORY_UNREADABLE,
        ):
            with self.subTest(health=health):
                self.assertEqual(
                    self._slot(health).compensation, COMPENSATION_ROLLBACK_OWED
                )

    def test_healthy_still_owes_nothing(self):
        self.assertEqual(
            self._slot(HEALTH_HEALTHY).compensation, COMPENSATION_NOT_NEEDED
        )


if __name__ == "__main__":
    unittest.main()
