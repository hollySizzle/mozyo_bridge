"""Unit tests for the stall classification -> prescription map (Redmine #15843).

The safety properties this US turns on are properties of the MAP, not of any one call
site, so they are asserted over the whole vocabulary rather than sampled: every class has
a prescription, nothing outside the fixed vocabulary is emittable, no class that could be
an upstream outage recommends relaunch, and patience is unreachable-to-escape without a
durable-record assertion from the caller.
"""

import unittest

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    APPLY_PRESENT_ONLY,
    CLASS_BUSY_LIKELY,
    CLASS_CONTENT_REFUSAL,
    CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
    CLASS_SCREEN_PROGRESSING,
    CLASS_SCREEN_UNREADABLE,
    CLASS_STARTUP_INTERACTION,
    CLASS_UNKNOWN,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    CLASS_UNSENT_COMPOSER,
    ESCALATABLE_CLASSES,
    EVIDENCE_BINARY_READ_UNRENDERED,
    RX_CONTEXT_RESET_REINJECT,
    RX_ENTER_ONLY_RETRY,
    RX_NO_ACTION,
    RX_OPERATOR_RESOLVES_SCREEN,
    RX_OWNER_ESCALATION,
    RX_PATIENT_WAIT_RETRY,
    STALL_CLASSES,
    STALL_PRESCRIPTIONS,
    UNRENDERED_ADMISSIBLE_CLASSES,
    Prescription,
    StallDispositionError,
    prescribe,
)


class PrescriptionMapTotalityTest(unittest.TestCase):
    def test_every_declared_class_has_a_prescription(self):
        for stall_class in STALL_CLASSES:
            with self.subTest(stall_class=stall_class):
                self.assertIn(prescribe(stall_class).action, STALL_PRESCRIPTIONS)

    def test_an_undeclared_class_is_refused_rather_than_defaulted(self):
        with self.assertRaises(StallDispositionError):
            prescribe("probably_stalled")

    def test_every_prescription_is_present_only(self):
        for stall_class in STALL_CLASSES:
            with self.subTest(stall_class=stall_class):
                self.assertEqual(prescribe(stall_class).posture, APPLY_PRESENT_ONLY)

    def test_an_applying_posture_cannot_be_constructed(self):
        with self.assertRaises(StallDispositionError):
            Prescription(RX_ENTER_ONLY_RETRY, posture="auto_apply")


class FixedPairingsTest(unittest.TestCase):
    """The remedies are mutually destructive, so each pairing is pinned explicitly."""

    def test_the_named_pairings(self):
        expected = {
            CLASS_SCREEN_PROGRESSING: RX_NO_ACTION,
            CLASS_BUSY_LIKELY: RX_NO_ACTION,
            CLASS_SCREEN_UNREADABLE: RX_NO_ACTION,
            CLASS_UNKNOWN: RX_NO_ACTION,
            CLASS_STARTUP_INTERACTION: RX_OPERATOR_RESOLVES_SCREEN,
            CLASS_CONTENT_REFUSAL: RX_CONTEXT_RESET_REINJECT,
            CLASS_UNSENT_COMPOSER: RX_ENTER_ONLY_RETRY,
            CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED: RX_PATIENT_WAIT_RETRY,
            CLASS_UNRESPONSIVE_INDETERMINATE: RX_PATIENT_WAIT_RETRY,
        }
        self.assertEqual(set(expected), STALL_CLASSES)
        for stall_class, action in expected.items():
            with self.subTest(stall_class=stall_class):
                self.assertEqual(prescribe(stall_class).action, action)


class PatientDispositionTest(unittest.TestCase):
    """#15843 owner intent: a possible outage is never answered with a relaunch."""

    def test_no_classification_alone_ever_names_relaunch(self):
        for stall_class in STALL_CLASSES:
            with self.subTest(stall_class=stall_class):
                self.assertFalse(prescribe(stall_class).relaunch_is_a_candidate)

    def test_outage_and_wedge_share_the_patient_prescription(self):
        # They are indistinguishable from outside, so they must not diverge into
        # different remedies — one of which would destroy the lane's work.
        self.assertEqual(
            prescribe(CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED).action,
            prescribe(CLASS_UNRESPONSIVE_INDETERMINATE).action,
        )

    def test_relaunch_becomes_a_candidate_only_after_the_caller_spends_patience(self):
        after = prescribe(
            CLASS_UNRESPONSIVE_INDETERMINATE, patient_window_exhausted=True
        )
        self.assertEqual(after.action, RX_OWNER_ESCALATION)
        self.assertTrue(after.relaunch_is_a_candidate)
        self.assertEqual(after.posture, APPLY_PRESENT_ONLY)

    def test_a_busy_or_unreadable_unit_never_ages_into_an_escalation(self):
        # Otherwise a twenty-minute test run would eventually be offered up for relaunch.
        for stall_class in STALL_CLASSES - ESCALATABLE_CLASSES:
            with self.subTest(stall_class=stall_class):
                self.assertNotEqual(
                    prescribe(stall_class, patient_window_exhausted=True).action,
                    RX_OWNER_ESCALATION,
                )

    def test_only_the_two_frozen_classes_are_escalatable(self):
        self.assertEqual(
            ESCALATABLE_CLASSES,
            {CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED, CLASS_UNRESPONSIVE_INDETERMINATE},
        )


class EvidenceTierTest(unittest.TestCase):
    def test_unrendered_evidence_may_only_assert_a_non_destructive_class(self):
        # The whole point of the tier: a wrong literal at this evidence level must change
        # what is reported and never what is recommended.
        self.assertTrue(UNRENDERED_ADMISSIBLE_CLASSES)
        for stall_class in UNRENDERED_ADMISSIBLE_CLASSES:
            with self.subTest(stall_class=stall_class):
                self.assertEqual(
                    prescribe(stall_class).action,
                    prescribe(CLASS_UNRESPONSIVE_INDETERMINATE).action,
                )

    def test_the_destructive_classes_are_not_admissible_on_unrendered_evidence(self):
        destructive = {
            CLASS_CONTENT_REFUSAL,  # its remedy discards a live session's context
            CLASS_UNSENT_COMPOSER,  # its remedy presses a key
            CLASS_STARTUP_INTERACTION,
        }
        self.assertFalse(destructive & UNRENDERED_ADMISSIBLE_CLASSES)
        self.assertEqual(EVIDENCE_BINARY_READ_UNRENDERED, "binary_read_unrendered")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
