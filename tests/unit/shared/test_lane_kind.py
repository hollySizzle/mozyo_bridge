"""Canonical lane-kind vocabulary contract (Redmine #13647)."""

from __future__ import annotations

import unittest

from mozyo_bridge.shared.lane_kind import (
    LANE_KIND_COORDINATOR,
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
    LANE_KINDS,
    LaneKindError,
    checked_lane_kind,
    is_lane_kind,
    optional_lane_kind,
)


class LaneKindVocabularyTest(unittest.TestCase):
    def test_exactly_three_canonical_tokens(self) -> None:
        self.assertEqual(
            LANE_KINDS,
            frozenset(
                {
                    LANE_KIND_COORDINATOR,
                    LANE_KIND_DELEGATED_COORDINATOR,
                    LANE_KIND_IMPLEMENTATION,
                }
            ),
        )
        # No `unknown` / alias member — the closed contract.
        self.assertEqual(len(LANE_KINDS), 3)

    def test_no_parent_child_grandchild_alias(self) -> None:
        # The machine vocabulary never grows the owner-facing 親/子/孫 aliases
        # (disposition j#85650 P3).
        for alias in ("parent", "child", "grandchild", "main", "sub", "nested"):
            self.assertFalse(is_lane_kind(alias))
            with self.assertRaises(LaneKindError):
                checked_lane_kind(alias, source="x")

    def test_optional_lane_kind_none_and_empty_are_absent(self) -> None:
        self.assertIsNone(optional_lane_kind(None, source="x"))
        self.assertIsNone(optional_lane_kind("", source="x"))
        self.assertEqual(
            optional_lane_kind("implementation", source="x"), "implementation"
        )
        with self.assertRaises(LaneKindError):
            optional_lane_kind("bogus", source="x")

    def test_single_source_of_truth_no_drift(self) -> None:
        # Adversarial drift guard: the cockpit delegation projection MUST re-export
        # the same object, never re-declare its own literals — else a downstream
        # consumer could diverge from the launch path's contract.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            delegation_projection,
        )

        self.assertIs(delegation_projection.LANE_KINDS, LANE_KINDS)
        self.assertEqual(delegation_projection.LANE_KIND_COORDINATOR, LANE_KIND_COORDINATOR)
        self.assertEqual(
            delegation_projection.LANE_KIND_DELEGATED_COORDINATOR,
            LANE_KIND_DELEGATED_COORDINATOR,
        )
        self.assertEqual(
            delegation_projection.LANE_KIND_IMPLEMENTATION, LANE_KIND_IMPLEMENTATION
        )

        # And the config schema validates its `by_lane_kind` keys against this exact set.
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
            LANE_PLACEMENT_LANE_KINDS,
        )

        self.assertIs(LANE_PLACEMENT_LANE_KINDS, LANE_KINDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
