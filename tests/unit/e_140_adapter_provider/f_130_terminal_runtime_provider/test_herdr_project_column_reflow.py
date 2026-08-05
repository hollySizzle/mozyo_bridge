"""Unit coverage for the project-column decision core (Redmine #14996 R2).

The actuation is exercised end-to-end against a real split tree in
``tests/regressions/test_issue_14996_project_column_geometry.py``; this file pins
the pure decisions underneath it — who is a coordinator pane, which pairs exist,
whether a tab is columnar, and which column a new pair splits — plus the parity
between the two layout producers the suite now owns.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.herdr_fake import render_pane_layout  # noqa: E402
from support.herdr_pane_tree import Leaf, Rect, Split, Tab  # noqa: E402

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E402,E501
    parse_pane_layout,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E402,E501
    COLUMN_OUTCOMES,
    COLUMN_SUCCESS_OUTCOMES,
    COLUMN_FAILED,
    ColumnAttach,
    CoordinatorPane,
    columnar_verdict,
    coordinator_panes_in,
    group_by_pair,
    plan_project_columns,
    resolve_project_groups,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

A = "ws-a"
B = "ws-b"
C = "ws-c"


def _row(workspace: str, role: str, locator: str, lane: str = "") -> dict:
    return {
        "name": encode_assigned_name(workspace, role, lane),
        "pane_id": locator,
        "agent_status": "idle",
    }


def _snapshot(tab: Tab):
    return parse_pane_layout(json.dumps(tab.layout_payload()))


def _pane(workspace: str, role: str, locator: str, lane: str = "default"):
    return CoordinatorPane(
        locator=locator,
        assigned_name=encode_assigned_name(workspace, role, lane),
        workspace_id=workspace,
        lane_id=lane,
        role=role,
    )


class CoordinatorPaneReadTest(unittest.TestCase):
    def test_only_decodable_panes_inside_the_target_workspace_are_read(self):
        rows = [
            _row(A, "codex", "w1:p2"),
            _row(A, "claude", "w1:p3"),
            _row(B, "codex", "w2:p2"),          # another herdr workspace
            {"name": "not-a-mzb1-name", "pane_id": "w1:p9"},  # an operator's own shell
            {"name": encode_assigned_name(B, "claude"), "pane_id": ""},  # no locator
        ]
        panes = coordinator_panes_in(rows, "w1")
        self.assertEqual([pane.locator for pane in panes], ["w1:p2", "w1:p3"])
        self.assertEqual({pane.workspace_id for pane in panes}, {A})

    def test_pairs_group_by_workspace_and_lane(self):
        panes = (
            _pane(A, "codex", "w1:p2"),
            _pane(A, "claude", "w1:p3"),
            _pane(B, "codex", "w1:p4", lane="delegated"),
        )
        groups = group_by_pair(panes)
        self.assertEqual(
            sorted(groups), [(A, "default"), (B, "delegated")]
        )
        self.assertEqual(len(groups[(A, "default")]), 2)

    def test_a_non_default_lane_is_its_own_pair_key(self):
        panes = (_pane(A, "codex", "w1:p2"), _pane(A, "codex", "w1:p5", lane="impl-1"))
        self.assertEqual(len(group_by_pair(panes)), 2)


class ColumnarVerdictTest(unittest.TestCase):
    def _two_columns(self):
        tab = Tab(tab_id="w1:t1", workspace_id="w1", bounds=Rect(0, 0, 54, 23))
        tab.root = Split(
            "right", 0.5,
            Split("down", 0.5, Leaf("w1:p2"), Leaf("w1:p3")),
            Split("down", 0.5, Leaf("w1:p4"), Leaf("w1:p5")),
        )
        return tab

    def _l_shape(self):
        tab = Tab(tab_id="w1:t1", workspace_id="w1", bounds=Rect(0, 0, 54, 23))
        tab.root = Split(
            "down", 0.5,
            Split("right", 0.5, Leaf("w1:p2"), Split("down", 0.5, Leaf("w1:p4"), Leaf("w1:p5"))),
            Leaf("w1:p3"),
        )
        return tab

    def _groups(self):
        return {
            (A, "default"): (_pane(A, "codex", "w1:p2"), _pane(A, "claude", "w1:p3")),
            (B, "default"): (_pane(B, "codex", "w1:p4"), _pane(B, "claude", "w1:p5")),
        }

    def test_two_full_height_columns_are_columnar(self):
        columnar, reason = columnar_verdict(_snapshot(self._two_columns()), self._groups())
        self.assertTrue(columnar, reason)

    def test_the_live_l_shape_is_not_columnar(self):
        columnar, reason = columnar_verdict(_snapshot(self._l_shape()), self._groups())
        self.assertFalse(columnar)
        self.assertIn("full-height column", reason)

    def test_a_pair_missing_from_the_tab_is_not_columnar(self):
        groups = dict(self._groups())
        groups[(C, "default")] = (_pane(C, "codex", "w1:p9"),)
        columnar, reason = columnar_verdict(_snapshot(self._two_columns()), groups)
        self.assertFalse(columnar)
        self.assertIn("w1:p9", reason)

    def test_equal_width_alone_is_not_enough_without_full_height(self):
        """The defect's lower pane had the right x; it spanned the whole width."""
        tab = Tab(tab_id="w1:t1", workspace_id="w1", bounds=Rect(0, 0, 54, 23))
        tab.root = Split(
            "down", 0.5,
            Split("down", 0.5, Leaf("w1:p2"), Leaf("w1:p4")),
            Split("down", 0.5, Leaf("w1:p3"), Leaf("w1:p5")),
        )
        columnar, _reason = columnar_verdict(_snapshot(tab), self._groups())
        self.assertFalse(columnar)


class ColumnPlanTest(unittest.TestCase):
    def _tab_with(self, columns: int):
        """``columns`` full-height project columns plus an appended L-shaped pair."""
        tab = Tab(tab_id="w1:t1", workspace_id="w1", bounds=Rect(0, 0, 54, 23))
        left = Split("down", 0.5, Leaf("w1:p2"), Leaf("w1:p3"))
        if columns == 2:
            left = Split(
                "right", 0.5, left,
                Split("down", 0.5, Leaf("w1:p6"), Leaf("w1:p7")),
            )
        tab.root = Split(
            "right", 0.5, left,
            Split("down", 0.5, Leaf("w1:p4"), Leaf("w1:p5")),
        )
        return tab

    def _groups(self, columns: int):
        groups = {
            (A, "default"): (_pane(A, "codex", "w1:p2"), _pane(A, "claude", "w1:p3")),
            (C, "default"): (_pane(C, "codex", "w1:p4"), _pane(C, "claude", "w1:p5")),
        }
        if columns == 2:
            groups[(B, "default")] = (
                _pane(B, "codex", "w1:p6"),
                _pane(B, "claude", "w1:p7"),
            )
        return groups

    def test_the_plan_detaches_the_new_pair_then_the_anchor_lower_pane(self):
        plan, refusal = plan_project_columns(
            _snapshot(self._tab_with(1)), self._groups(1), (C, "default"),
            ["w1:p4", "w1:p5"],
        )
        self.assertEqual(refusal, "")
        self.assertEqual(plan.detach, ("w1:p5", "w1:p4", "w1:p3"))
        self.assertEqual(plan.anchor_pane, "w1:p2")
        self.assertEqual(
            plan.attach,
            (
                ColumnAttach(pane="w1:p4", direction="right", target="w1:p2"),
                ColumnAttach(pane="w1:p5", direction="down", target="w1:p4"),
                ColumnAttach(pane="w1:p3", direction="down", target="w1:p2"),
            ),
        )

    def test_the_anchor_is_the_rightmost_existing_column(self):
        plan, refusal = plan_project_columns(
            _snapshot(self._tab_with(2)), self._groups(2), (C, "default"),
            ["w1:p4", "w1:p5"],
        )
        self.assertEqual(refusal, "")
        # Project B sits right of project A, so the new column splits B and leaves
        # A's column untouched (j#99833 acceptance 3: no unrelated reordering).
        self.assertEqual(plan.anchor_pane, "w1:p6")
        self.assertEqual(plan.detach, ("w1:p5", "w1:p4", "w1:p7"))
        self.assertNotIn("w1:p2", plan.detach)
        self.assertNotIn("w1:p3", plan.detach)

    def test_a_partial_launch_appends_no_column(self):
        plan, refusal = plan_project_columns(
            _snapshot(self._tab_with(1)), self._groups(1), (C, "default"), ["w1:p4"]
        )
        self.assertIsNone(plan)
        self.assertIn("full fresh pair", refusal)

    def test_an_unrecognised_foreign_group_is_refused_rather_than_reshaped(self):
        groups = self._groups(1)
        groups[(A, "default")] = groups[(A, "default")] + (
            _pane(A, "codex", "w1:p8", lane="default"),
        )
        tab = self._tab_with(1)
        tab.subdivide("w1:p3", "down", "w1:p8")
        plan, refusal = plan_project_columns(
            _snapshot(tab), groups, (C, "default"), ["w1:p4", "w1:p5"]
        )
        self.assertIsNone(plan)
        self.assertIn("does not recognise", refusal)

    def test_a_launched_pane_outside_the_inventory_is_refused(self):
        plan, refusal = plan_project_columns(
            _snapshot(self._tab_with(1)), self._groups(1), (C, "default"),
            ["w1:p4", "w1:pGHOST"],
        )
        self.assertIsNone(plan)
        self.assertIn("no decodable pair identity", refusal)


class ProjectGroupAuthorityTest(unittest.TestCase):
    """Review j#99885 finding_2 / finding_3 — what may become a project pair.

    ``resolve_project_groups`` is the only producer a plan may consume; these pin
    the pure half of its contract (the durable lane-kind join is exercised against
    a real store in the #14996 regression).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _rows(self, *entries):
        return [
            {"name": encode_assigned_name(ws, role, lane), "pane_id": locator,
             "agent_status": "idle"}
            for ws, role, lane, locator in entries
        ]

    def test_a_malformed_group_is_refused_without_opening_any_store(self):
        """The pure phases run first, so a store read is never the cheapest refusal.

        The heavier authorities — the lifecycle store, the workspace registry and
        the attestation store — are only consulted once the inventory is
        well-formed on its face. Acceptance itself is proved end-to-end against
        those real stores in ``tests/regressions/test_issue_14996_*``.
        """
        rows = self._rows((A, "codex", "", "w1:p2"), (A, "nethack", "", "w1:p3"))
        opened = []
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_project_column_authority.load_lane_lifecycle_readonly",
            side_effect=lambda **_: opened.append("lifecycle") or (),
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_project_column_authority.herdr_workspace_segment",
            side_effect=lambda *a, **k: opened.append("registry") or "",
        ):
            groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertEqual(groups, {})
        self.assertIn("unrecognised provider", refusal)
        self.assertEqual(opened, [])

    def test_the_configured_top_pair_is_refused_before_any_foreign_join(self):
        rows = self._rows((A, "codex", "", "w1:p2"), (A, "claude", "", "w1:p3"))
        groups, refusal = resolve_project_groups(
            rows, "w1", home=self.home, top_workspace_id=A
        )
        self.assertEqual(groups, {})
        self.assertIn("configured top coordinator", refusal)

    def test_an_unrecognised_provider_token_is_refused(self):
        rows = self._rows((A, "codex", "", "w1:p2"), (A, "nethack", "", "w1:p3"))
        groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertEqual(groups, {})
        self.assertIn("unrecognised provider", refusal)

    def test_more_panes_than_a_pair_can_hold_is_refused(self):
        rows = self._rows(
            (A, "codex", "", "w1:p2"), (A, "claude", "", "w1:p3"),
        ) + [{"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p9",
              "agent_status": "idle"}]
        groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertEqual(groups, {})
        self.assertIn("duplicate provider", refusal)

    def test_a_group_of_one_live_provider_passes_the_shape_phase(self):
        """The disputed half of finding_3 (verdict j#99888 / Answer j#99900).

        Cardinality alone does not refuse. The pane still has to carry positive
        evidence before it can be moved beside, which is why this stops at the
        foreign-evidence phase rather than at the shape one.
        """
        rows = self._rows((A, "codex", "", "w1:p2"))
        _groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertNotIn("pair", refusal)
        self.assertIn("w1:p2", refusal)

    def test_a_named_lane_with_no_durable_kind_refuses_the_whole_set(self):
        rows = self._rows(
            (A, "codex", "", "w1:p2"), (A, "claude", "", "w1:p3"),
            (A, "codex", "impl-1", "w1:p4"),
        )
        groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertEqual(groups, {})
        self.assertIn("no durable lane-kind", refusal)

    def test_an_unreadable_lane_kind_authority_refuses_rather_than_defaults(self):
        rows = self._rows((A, "codex", "impl-1", "w1:p2"))
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_project_column_authority.load_lane_lifecycle_readonly",
            return_value=None,
        ):
            groups, refusal = resolve_project_groups(rows, "w1", home=self.home)
        self.assertEqual(groups, {})
        self.assertIn("unreadable", refusal)


class ColumnVocabularyTest(unittest.TestCase):
    def test_the_success_set_is_the_vocabulary_minus_failed(self):
        self.assertEqual(
            set(COLUMN_OUTCOMES), COLUMN_SUCCESS_OUTCOMES | {COLUMN_FAILED}
        )

    def test_failed_is_the_only_non_success_token(self):
        self.assertNotIn(COLUMN_FAILED, COLUMN_SUCCESS_OUTCOMES)
        self.assertEqual(len(COLUMN_SUCCESS_OUTCOMES), 3)


class LayoutProducerParityTest(unittest.TestCase):
    """The tree producer and the flat pair producer must describe one geometry.

    Two fakes that each render herdr's layout drift, and the one that drifts stops
    testing the parser it was written for. The tree is a superset — it can also
    render a container that grew past a pair — so parity is asserted where both
    can speak: the one-split case.
    """

    def _tree_pair(self, direction: str, ratio: float):
        extent, cross = 100, 80
        bounds = (
            Rect(0, 0, cross, extent) if direction == "down" else Rect(0, 0, extent, cross)
        )
        tab = Tab(tab_id="t1", workspace_id="w1", bounds=bounds)
        tab.root = Split(direction, ratio, Leaf("w1:p2"), Leaf("w1:p3"))
        return tab

    def _fields(self, payload):
        layout = payload["result"]["layout"]
        return (
            [(pane["pane_id"], pane["rect"]) for pane in layout["panes"]],
            [(split["direction"], split["ratio"], split["rect"]) for split in layout["splits"]],
        )

    def test_a_pair_renders_identically_in_both_producers(self):
        for direction in ("down", "right"):
            for ratio in (0.5, 0.4, 0.7):
                with self.subTest(direction=direction, ratio=ratio):
                    flat = render_pane_layout(
                        pane_ids=["w1:p2", "w1:p3"], direction=direction, ratio=ratio,
                        extent=100, cross=80,
                    )
                    tree = self._tree_pair(direction, ratio).layout_payload()
                    self.assertEqual(self._fields(flat), self._fields(tree))

    def test_both_producers_parse_into_the_same_snapshot(self):
        flat = parse_pane_layout(
            json.dumps(
                render_pane_layout(
                    pane_ids=["w1:p2", "w1:p3"], direction="down", ratio=0.5,
                    extent=100, cross=80,
                )
            )
        )
        tree = _snapshot(self._tree_pair("down", 0.5))
        self.assertEqual(dict(flat.panes), dict(tree.panes))
        self.assertEqual(
            [(s.direction, s.ratio, s.rect) for s in flat.splits],
            [(s.direction, s.ratio, s.rect) for s in tree.splits],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
