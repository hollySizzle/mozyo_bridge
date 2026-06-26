"""Delegation projection metadata read model tests (Redmine #12465 / US #12454).

Pins the derived ``lane_kind`` / ``delegation_depth`` / ``delegation_parent`` /
``delegation_root`` shape, the re-derivability from durable anchors, the
fail-closed boundaries, and the non-routing / read-model boundary from
``vibes/docs/logics/delegated-coordinator-cockpit-display.md``.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.delegation_projection import (
    LANE_KIND_COORDINATOR,
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
    LANE_KINDS,
    MAX_DELEGATION_DEPTH,
    OPTION_DELEGATION_DEPTH,
    OPTION_DELEGATION_PARENT,
    OPTION_DELEGATION_ROOT,
    OPTION_LANE_KIND,
    DelegationProjection,
    DelegationProjectionError,
    DelegationSource,
    delegation_user_options,
    derive_delegation_projection,
    derive_delegation_tree,
)

# A representative parent -> delegated coordinator -> grandchild lane tree, with
# unit_ids in the cockpit ``unit:<host>:<workspace_id>:<lane_id>`` convention.
ROOT_ID = "unit:local:gk3500:default"
DELEGATED_ID = "unit:local:mozyo:delegated"
GRANDCHILD_ID = "unit:local:mozyo:issue_12465"


def _three_level_sources() -> list[DelegationSource]:
    return [
        DelegationSource(
            unit_id=ROOT_ID,
            lane_kind=LANE_KIND_COORDINATOR,
            delegation_parent=None,
            source_refs=("redmine:#12454#journal-63761",),
        ),
        DelegationSource(
            unit_id=DELEGATED_ID,
            lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
            delegation_parent=ROOT_ID,
            source_refs=("redmine:#12465#journal-63763",),
        ),
        DelegationSource(
            unit_id=GRANDCHILD_ID,
            lane_kind=LANE_KIND_IMPLEMENTATION,
            delegation_parent=DELEGATED_ID,
            source_refs=("redmine:#12465#journal-63770",),
        ),
    ]


class DeriveTreeTest(unittest.TestCase):
    def test_three_level_depth_and_root(self) -> None:
        tree = derive_delegation_tree(_three_level_sources())

        root = tree[ROOT_ID]
        self.assertEqual(root.delegation_depth, 0)
        self.assertIsNone(root.delegation_parent)
        # The root is its own delegation_root.
        self.assertEqual(root.delegation_root, ROOT_ID)
        self.assertEqual(root.lane_kind, LANE_KIND_COORDINATOR)

        delegated = tree[DELEGATED_ID]
        self.assertEqual(delegated.delegation_depth, 1)
        self.assertEqual(delegated.delegation_parent, ROOT_ID)
        self.assertEqual(delegated.delegation_root, ROOT_ID)
        self.assertEqual(delegated.lane_kind, LANE_KIND_DELEGATED_COORDINATOR)

        grandchild = tree[GRANDCHILD_ID]
        self.assertEqual(grandchild.delegation_depth, 2)
        self.assertEqual(grandchild.delegation_parent, DELEGATED_ID)
        # The grandchild's root is the top coordinator, not its direct parent.
        self.assertEqual(grandchild.delegation_root, ROOT_ID)
        self.assertEqual(grandchild.lane_kind, LANE_KIND_IMPLEMENTATION)

    def test_re_derivable_from_durable_parent_pointers_only(self) -> None:
        # The depth/root are a function of the durable delegation_parent chain
        # alone — no pane option / window title is consulted. Shuffling the input
        # order (a durable record carries no positional meaning) yields the same
        # derived records.
        sources = _three_level_sources()
        shuffled = [sources[2], sources[0], sources[1]]
        self.assertEqual(
            derive_delegation_tree(sources),
            derive_delegation_tree(shuffled),
        )

    def test_pure_and_deterministic(self) -> None:
        self.assertEqual(
            derive_delegation_tree(_three_level_sources()),
            derive_delegation_tree(_three_level_sources()),
        )

    def test_two_independent_roots(self) -> None:
        # Two unrelated coordinators in one source set each resolve to their own
        # root at depth 0; the tree is not assumed to be single-rooted.
        other_root = "unit:local:other:default"
        sources = _three_level_sources() + [
            DelegationSource(
                unit_id=other_root,
                lane_kind=LANE_KIND_COORDINATOR,
                delegation_parent=None,
            )
        ]
        tree = derive_delegation_tree(sources)
        self.assertEqual(tree[other_root].delegation_root, other_root)
        self.assertEqual(tree[GRANDCHILD_ID].delegation_root, ROOT_ID)


class FailClosedTest(unittest.TestCase):
    def test_unknown_parent_pointer_fails_closed(self) -> None:
        sources = [
            DelegationSource(
                unit_id=DELEGATED_ID,
                lane_kind=LANE_KIND_DELEGATED_COORDINATOR,
                delegation_parent="unit:local:gk3500:missing",
            )
        ]
        with self.assertRaises(DelegationProjectionError):
            derive_delegation_tree(sources)

    def test_cycle_fails_closed(self) -> None:
        a = "unit:local:ws:a"
        b = "unit:local:ws:b"
        sources = [
            DelegationSource(unit_id=a, lane_kind=LANE_KIND_COORDINATOR, delegation_parent=b),
            DelegationSource(unit_id=b, lane_kind=LANE_KIND_COORDINATOR, delegation_parent=a),
        ]
        with self.assertRaises(DelegationProjectionError):
            derive_delegation_tree(sources)

    def test_depth_beyond_shallow_maximum_fails_closed(self) -> None:
        # A 4-level chain (depth 3) exceeds the shallow-delegation maximum.
        units = ["unit:local:ws:l0", "unit:local:ws:l1", "unit:local:ws:l2", "unit:local:ws:l3"]
        sources = [
            DelegationSource(unit_id=units[0], lane_kind=LANE_KIND_COORDINATOR, delegation_parent=None),
            DelegationSource(unit_id=units[1], lane_kind=LANE_KIND_DELEGATED_COORDINATOR, delegation_parent=units[0]),
            DelegationSource(unit_id=units[2], lane_kind=LANE_KIND_IMPLEMENTATION, delegation_parent=units[1]),
            DelegationSource(unit_id=units[3], lane_kind=LANE_KIND_IMPLEMENTATION, delegation_parent=units[2]),
        ]
        self.assertEqual(MAX_DELEGATION_DEPTH, 2)
        with self.assertRaises(DelegationProjectionError):
            derive_delegation_tree(sources)

    def test_off_contract_lane_kind_fails_closed(self) -> None:
        # Any token outside the closed contract enum — including the literal
        # "unknown" — must fail closed rather than be emitted as a projection /
        # @mozyo_lane_kind cache value (Redmine #12465 review j#63800).
        self.assertNotIn("unknown", LANE_KINDS)
        for off_contract in ("manager", "unknown", "", "Coordinator"):
            with self.subTest(lane_kind=off_contract):
                sources = [
                    DelegationSource(
                        unit_id=ROOT_ID,
                        lane_kind=off_contract,
                        delegation_parent=None,
                    )
                ]
                with self.assertRaises(DelegationProjectionError):
                    derive_delegation_tree(sources)

    def test_duplicate_unit_id_fails_closed(self) -> None:
        sources = [
            DelegationSource(unit_id=ROOT_ID, lane_kind=LANE_KIND_COORDINATOR),
            DelegationSource(unit_id=ROOT_ID, lane_kind=LANE_KIND_COORDINATOR),
        ]
        with self.assertRaises(DelegationProjectionError):
            derive_delegation_tree(sources)


class DeriveOneTest(unittest.TestCase):
    def test_single_lane(self) -> None:
        projection = derive_delegation_projection(GRANDCHILD_ID, _three_level_sources())
        self.assertEqual(projection.delegation_depth, 2)
        self.assertEqual(projection.delegation_root, ROOT_ID)

    def test_missing_unit_fails_closed(self) -> None:
        with self.assertRaises(DelegationProjectionError):
            derive_delegation_projection("unit:local:ws:absent", _three_level_sources())


class UserOptionsTest(unittest.TestCase):
    def test_option_mapping_for_grandchild(self) -> None:
        projection = derive_delegation_projection(GRANDCHILD_ID, _three_level_sources())
        options = delegation_user_options(projection)
        self.assertEqual(options[OPTION_LANE_KIND], LANE_KIND_IMPLEMENTATION)
        self.assertEqual(options[OPTION_DELEGATION_ROOT], ROOT_ID)
        self.assertEqual(options[OPTION_DELEGATION_PARENT], DELEGATED_ID)
        self.assertEqual(options[OPTION_DELEGATION_DEPTH], "2")

    def test_root_parent_option_is_empty_string(self) -> None:
        projection = derive_delegation_projection(ROOT_ID, _three_level_sources())
        options = delegation_user_options(projection)
        # Root has no parent: explicit empty string, not a missing key.
        self.assertIn(OPTION_DELEGATION_PARENT, options)
        self.assertEqual(options[OPTION_DELEGATION_PARENT], "")
        self.assertEqual(options[OPTION_DELEGATION_DEPTH], "0")

    def test_option_values_are_all_strings(self) -> None:
        # The projection cache is a tmux user-option mapping; every value must be
        # a string a downstream writer can set verbatim.
        projection = derive_delegation_projection(DELEGATED_ID, _three_level_sources())
        for value in delegation_user_options(projection).values():
            self.assertIsInstance(value, str)


class NonAuthorityBoundaryTest(unittest.TestCase):
    def test_projection_carries_no_routing_or_close_authority_field(self) -> None:
        # The projection is display/audit only. It must not grow a field that
        # would let display metadata become routing / handoff / approval / close
        # authority (acceptance: "routing / handoff / approval / close authority
        # を持つ field を増やさない").
        field_names = {f.name for f in dataclasses.fields(DelegationProjection)}
        forbidden_substrings = (
            "target",
            "route",
            "send",
            "handoff",
            "approval",
            "close",
            "pane",
            "window",
            "session",
        )
        for name in field_names:
            for token in forbidden_substrings:
                self.assertNotIn(
                    token,
                    name,
                    msg=f"projection field {name!r} looks like routing/authority state",
                )

    def test_payload_round_trips_display_fields(self) -> None:
        projection = derive_delegation_projection(DELEGATED_ID, _three_level_sources())
        payload = projection.as_payload()
        self.assertEqual(payload["unit_id"], DELEGATED_ID)
        self.assertEqual(payload["lane_kind"], LANE_KIND_DELEGATED_COORDINATOR)
        self.assertEqual(payload["delegation_parent"], ROOT_ID)
        self.assertEqual(payload["delegation_root"], ROOT_ID)
        self.assertEqual(payload["delegation_depth"], 1)
        self.assertEqual(payload["source_refs"], ["redmine:#12465#journal-63763"])

    def test_emitted_lane_kind_is_always_a_contract_value(self) -> None:
        # Every derived projection / option-cache lane_kind is one of the closed
        # contract enum values; "unknown" is never carried (review j#63800).
        for projection in derive_delegation_tree(_three_level_sources()).values():
            self.assertIn(projection.lane_kind, LANE_KINDS)
            options = delegation_user_options(projection)
            self.assertIn(options[OPTION_LANE_KIND], LANE_KINDS)


if __name__ == "__main__":
    unittest.main()
