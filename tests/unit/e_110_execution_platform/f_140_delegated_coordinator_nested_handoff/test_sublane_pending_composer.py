"""Pending-composer classifier tests (Redmine #13763 j#78011 contract 2 / 8).

The classifier is the only thing that decides whether an uncorrelatable composer is a
quarantine candidate, so its precedence is the safety boundary: an unreadable inventory,
a mismatched generation, an unattested identity, or a working agent must all win over
"there is pending text here", and a composer that DOES correlate to a known delivery
marker must route back to the existing q-enter rail rather than become a close candidate.

The signal type is also the body fence (contract 8): only content-free facts cross it.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    AGENT_WORKING,
    AMBIGUOUS,
    CORRELATED_KNOWN_MARKER,
    GENERATION_MISMATCH,
    IDENTITY_UNATTESTED,
    INVENTORY_UNREADABLE,
    NO_PENDING_COMPOSER,
    UNCORRELATED,
    PendingComposerSignal,
    classify_pending_composer,
)

MARKER = "[mozyo:handoff:source=redmine:issue=13763:journal=78011:kind=implementation_request:to=claude]"
OTHER_MARKER = "[mozyo:handoff:source=redmine:issue=13683:journal=77502:kind=review_request:to=claude]"


def _signal(**kw) -> PendingComposerSignal:
    """A healthy, attested, idle receiver holding an uncorrelatable composer."""
    base = dict(
        inventory_readable=True,
        has_pending=True,
        agent_state="idle",
        identity_attested=True,
        generation_matches=True,
        correlated_marker_ids=(),
        correlation_ambiguous=False,
    )
    base.update(kw)
    return PendingComposerSignal(**base)


class ClassificationTest(unittest.TestCase):
    def test_uncorrelated_pending_text_is_the_quarantine_candidate(self) -> None:
        result = classify_pending_composer(_signal())
        self.assertEqual(result.label, UNCORRELATED)
        self.assertTrue(result.quarantine_candidate)
        self.assertFalse(result.q_enter_recommended)
        self.assertFalse(result.blocked)

    def test_known_marker_routes_to_q_enter_and_is_never_a_candidate(self) -> None:
        # The whole point of contract 2: a composer we CAN correlate to a delivery
        # marker is drivable through the existing rail. Closing it would destroy an
        # input we know how to submit.
        result = classify_pending_composer(
            _signal(correlated_marker_ids=(MARKER,))
        )
        self.assertEqual(result.label, CORRELATED_KNOWN_MARKER)
        self.assertEqual(result.correlated_marker_id, MARKER)
        self.assertTrue(result.q_enter_recommended)
        self.assertFalse(result.quarantine_candidate)

    def test_two_correlated_markers_are_ambiguous_not_known(self) -> None:
        result = classify_pending_composer(
            _signal(correlated_marker_ids=(MARKER, OTHER_MARKER))
        )
        self.assertEqual(result.label, AMBIGUOUS)
        self.assertTrue(result.quarantine_candidate)
        # No single action identity may be claimed for an ambiguous composer.
        self.assertEqual(result.correlated_marker_id, "")

    def test_repeated_marker_id_is_one_known_action_not_ambiguous(self) -> None:
        # The same marker rendered twice (redraw / wrap) is still ONE action identity.
        result = classify_pending_composer(
            _signal(correlated_marker_ids=(MARKER, MARKER))
        )
        self.assertEqual(result.label, CORRELATED_KNOWN_MARKER)

    def test_adapter_flagged_ambiguity_wins_over_a_single_correlation(self) -> None:
        # The adapter saw more markers in the composer than it could correlate: the
        # single correlated one does not make the input safely drivable.
        result = classify_pending_composer(
            _signal(correlated_marker_ids=(MARKER,), correlation_ambiguous=True)
        )
        self.assertEqual(result.label, AMBIGUOUS)
        self.assertTrue(result.quarantine_candidate)

    def test_empty_composer_is_not_a_candidate(self) -> None:
        result = classify_pending_composer(_signal(has_pending=False))
        self.assertEqual(result.label, NO_PENDING_COMPOSER)
        self.assertFalse(result.quarantine_candidate)
        self.assertTrue(result.blocked)

    def test_working_agent_blocks_even_with_uncorrelated_text(self) -> None:
        for state in ("busy", "Working", "  BUSY "):
            with self.subTest(state=state):
                result = classify_pending_composer(_signal(agent_state=state))
                self.assertEqual(result.label, AGENT_WORKING)
                self.assertFalse(result.quarantine_candidate)

    def test_unattested_identity_blocks(self) -> None:
        result = classify_pending_composer(_signal(identity_attested=False))
        self.assertEqual(result.label, IDENTITY_UNATTESTED)
        self.assertFalse(result.quarantine_candidate)

    def test_generation_mismatch_blocks(self) -> None:
        result = classify_pending_composer(_signal(generation_matches=False))
        self.assertEqual(result.label, GENERATION_MISMATCH)
        self.assertFalse(result.quarantine_candidate)

    def test_unreadable_inventory_fails_closed(self) -> None:
        result = classify_pending_composer(
            _signal(inventory_readable=False, generation_matches=False)
        )
        self.assertEqual(result.label, INVENTORY_UNREADABLE)
        self.assertFalse(result.quarantine_candidate)

    def test_unknown_pending_state_fails_closed_rather_than_assuming_empty(self) -> None:
        # The adapter could not tell whether a composer holds text. "Unknown" must
        # never degrade into "empty, therefore nothing to lose".
        result = classify_pending_composer(_signal(has_pending=None))
        self.assertEqual(result.label, INVENTORY_UNREADABLE)
        self.assertFalse(result.quarantine_candidate)

    def test_precedence_is_fail_closed_when_several_faults_coincide(self) -> None:
        # Every blocking fault at once, plus a correlated marker that would otherwise
        # look drivable: the most conservative classification wins.
        result = classify_pending_composer(
            _signal(
                inventory_readable=False,
                generation_matches=False,
                identity_attested=False,
                agent_state="busy",
                correlated_marker_ids=(MARKER,),
            )
        )
        self.assertEqual(result.label, INVENTORY_UNREADABLE)
        self.assertTrue(result.blocked)


class BodyFenceTest(unittest.TestCase):
    def test_signal_carries_no_composer_body_field(self) -> None:
        # Contract 8: the pure classifier boundary exposes fixed classification facts
        # only — no body, hash, length, or excerpt field exists to persist. Redmine #15193
        # adds `generation_axes`, a tuple drawn from a closed axis vocabulary, which keeps
        # the fence: it names WHICH generation check failed, never any observed value.
        fields = {f.name for f in dataclasses.fields(PendingComposerSignal)}
        self.assertEqual(
            fields,
            {
                "inventory_readable",
                "has_pending",
                "agent_state",
                "identity_attested",
                "generation_matches",
                "correlated_marker_ids",
                "correlation_ambiguous",
                "generation_axes",
            },
        )

    def test_payload_records_classification_and_action_only(self) -> None:
        payload = classify_pending_composer(
            _signal(correlated_marker_ids=(MARKER,))
        ).as_payload()
        self.assertEqual(
            set(payload),
            {
                "classification",
                "correlated_marker_id",
                "q_enter_recommended",
                "quarantine_candidate",
                "blocked",
                "pending_observed",
                "generation_axes",
                "generation_mismatch_with_pending",
            },
        )
        # The only free-form value is a delivery-marker identity we already own.
        self.assertEqual(payload["correlated_marker_id"], MARKER)


class CoObservedFactsTest(unittest.TestCase):
    """Redmine #15193: a losing precedence branch must stop discarding what it observed."""

    def test_generation_mismatch_still_reports_the_pending_fact(self) -> None:
        # THE #15193 defect: this receiver holds a real unsent input, but the collapsed
        # label said only `generation_mismatch`, so quarantine-inspect reported
        # `not_quarantine_candidate` while hibernate blocked on `composer_pending_real`.
        result = classify_pending_composer(
            _signal(generation_matches=False, has_pending=True, generation_axes=("pair",))
        )
        self.assertEqual(result.label, GENERATION_MISMATCH)
        # The verdict is unchanged — no candidacy is widened.
        self.assertFalse(result.quarantine_candidate)
        # ...but the co-observed facts now survive.
        self.assertTrue(result.pending_observed)
        self.assertEqual(result.generation_axes, ("pair",))
        self.assertTrue(result.generation_mismatch_with_pending)

    def test_generation_mismatch_without_pending_is_not_the_disposition_shape(self) -> None:
        result = classify_pending_composer(
            _signal(generation_matches=False, has_pending=False, generation_axes=("identity",))
        )
        self.assertEqual(result.label, GENERATION_MISMATCH)
        self.assertFalse(result.generation_mismatch_with_pending)

    def test_unreadable_composer_is_never_the_disposition_shape(self) -> None:
        # An unprovable pending fact must not open a path that could discard it.
        result = classify_pending_composer(
            _signal(generation_matches=False, has_pending=None, generation_axes=("pair",))
        )
        self.assertEqual(result.label, GENERATION_MISMATCH)
        self.assertIsNone(result.pending_observed)
        self.assertFalse(result.generation_mismatch_with_pending)

    def test_axes_are_canonically_ordered_and_unknown_tokens_dropped(self) -> None:
        result = classify_pending_composer(
            _signal(
                generation_matches=False,
                generation_axes=("pair", "identity", "not_a_real_axis"),
            )
        )
        self.assertEqual(result.generation_axes, ("identity", "pair"))

    def test_axes_are_not_reported_on_a_label_they_did_not_decide(self) -> None:
        # The generation matched, so any axis tuple the adapter passed describes nothing
        # live; reporting it would invite reading a spent observation as evidence.
        result = classify_pending_composer(
            _signal(generation_matches=True, generation_axes=("pair",))
        )
        self.assertEqual(result.label, UNCORRELATED)
        self.assertEqual(result.generation_axes, ())

    def test_inventory_unreadable_outranks_the_mismatch_and_reports_no_axes(self) -> None:
        result = classify_pending_composer(
            _signal(inventory_readable=False, generation_matches=False, generation_axes=("pair",))
        )
        self.assertEqual(result.label, INVENTORY_UNREADABLE)
        self.assertEqual(result.generation_axes, ())
        self.assertFalse(result.generation_mismatch_with_pending)


if __name__ == "__main__":
    unittest.main()
