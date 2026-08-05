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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E402,E501
    group_by_pair,
)

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E402,E501
    parse_pane_layout,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DISPOSITION_ACTIVE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E402,E501
    ROW_DISPOSITIONS,
    ROW_IN_SCOPE,
    ROW_OUT_OF_SCOPE,
    ROW_REFUSED,
    LaneFact,
    OwnSlot,
    ProjectColumnAuthority,
    classify_inventory_row,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E402,E501
    COLUMN_OUTCOMES,
    COLUMN_SUCCESS_OUTCOMES,
    COLUMN_FAILED,
    ColumnAttach,
    CoordinatorPane,
    columnar_verdict,
    coordinator_panes_in,
    plan_project_columns,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

A = "ws-a"
B = "ws-b"
C = "ws-c"


def _row(workspace: str, role: str, locator: str, lane: str = "", **over) -> dict:
    row = {
        "name": encode_assigned_name(workspace, role, lane),
        "pane_id": locator,
        "agent_status": "idle",
    }
    row.update(over)
    return row


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


#: The two things a row can say about where it lives. The classifier's decision is
#: taken over the FULL product of these, so the table below must hold every cell —
#: and the test asserts that, because a table is a claim and the claim is what is
#: checked (review j#99960 finding_1: the previous guard only covered the OUTCOME
#: vocabulary, which says nothing about the input space).
#: ``non_string`` is an axis value, not an edge case: ``_norm`` is
#: ``str(value).strip()``, so a list / dict / int / bool does not stay malformed —
#: it becomes a perfectly good workspace id, and four such rows classified as
#: out-of-scope while six panes moved past them (review j#99971 finding_1). An axis
#: that omits the SHAPE a field arrives in cannot cover the input space, however
#: completely the grid covers the axis.
_DECLARED_KINDS = ("absent", "target", "foreign", "non_string")
_LOCATOR_KINDS = (
    "absent", "unparseable", "target", "foreign", "other_foreign", "non_string",
)

#: ``(declared kind, locator kind) -> (row, expected disposition)``. ``other_foreign``
#: is a SECOND foreign workspace, which is what makes "declared foreign + locator a
#: different foreign" expressible at all — the cell that used to pass as out-of-scope
#: while six panes moved around the row it described.
_ROW_GRID = {
    ("absent", "absent"): ({"name": "n", "pane_id": ""}, ROW_REFUSED),
    ("absent", "unparseable"): ({"name": "n", "pane_id": "nocolon"}, ROW_REFUSED),
    ("absent", "target"): (
        {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2"}, ROW_IN_SCOPE),
    ("absent", "foreign"): (
        {"name": encode_assigned_name(B, "codex"), "pane_id": "w2:p2"},
        ROW_OUT_OF_SCOPE),
    ("absent", "other_foreign"): (
        {"name": encode_assigned_name(B, "codex"), "pane_id": "w3:p2"},
        ROW_OUT_OF_SCOPE),
    ("target", "absent"): (
        {"name": "n", "pane_id": "", "workspace_id": "w1"}, ROW_REFUSED),
    ("target", "unparseable"): (
        {"name": "n", "pane_id": "nocolon", "workspace_id": "w1"}, ROW_REFUSED),
    ("target", "target"): (
        {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
         "workspace_id": "w1"}, ROW_IN_SCOPE),
    ("target", "foreign"): (
        {"name": "n", "pane_id": "w2:p9", "workspace_id": "w1"}, ROW_REFUSED),
    ("target", "other_foreign"): (
        {"name": "n", "pane_id": "w3:p9", "workspace_id": "w1"}, ROW_REFUSED),
    ("foreign", "absent"): (
        {"name": "n", "pane_id": "", "workspace_id": "w2"}, ROW_OUT_OF_SCOPE),
    ("foreign", "unparseable"): (
        {"name": "n", "pane_id": "nocolon", "workspace_id": "w2"}, ROW_OUT_OF_SCOPE),
    ("foreign", "target"): (
        {"name": "n", "pane_id": "w1:p9", "workspace_id": "w2"}, ROW_REFUSED),
    ("foreign", "foreign"): (
        {"name": encode_assigned_name(B, "codex"), "pane_id": "w2:p2",
         "workspace_id": "w2"}, ROW_OUT_OF_SCOPE),
    ("foreign", "other_foreign"): (
        {"name": "n", "pane_id": "w3:p9", "workspace_id": "w2"}, ROW_REFUSED),
    ("absent", "non_string"): ({"name": "n", "pane_id": 17}, ROW_REFUSED),
    ("target", "non_string"): (
        {"name": "n", "pane_id": ["w1:p2"], "workspace_id": "w1"}, ROW_REFUSED),
    ("foreign", "non_string"): (
        {"name": "n", "pane_id": {"p": 1}, "workspace_id": "w2"}, ROW_REFUSED),
    ("non_string", "absent"): (
        {"name": "n", "pane_id": "", "workspace_id": []}, ROW_REFUSED),
    ("non_string", "unparseable"): (
        {"name": "n", "pane_id": "nocolon", "workspace_id": {}}, ROW_REFUSED),
    ("non_string", "target"): (
        {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
         "workspace_id": 17}, ROW_REFUSED),
    ("non_string", "foreign"): (
        {"name": "n", "pane_id": "w2:p2", "workspace_id": True}, ROW_REFUSED),
    ("non_string", "other_foreign"): (
        {"name": "n", "pane_id": "w3:p2", "workspace_id": 3.5}, ROW_REFUSED),
    ("non_string", "non_string"): (
        {"name": "n", "pane_id": 1, "workspace_id": []}, ROW_REFUSED),
}

#: Shapes outside the declared x locator grid entirely.
_ROW_SHAPES = (
    ("not a mapping", ["not", "a", "row"], ROW_REFUSED),
)


class InventoryRowClassificationTest(unittest.TestCase):
    """Every row lands on exactly one disposition — the grid IS the spec.

    Three reviews found a branch that had been written as a narrower question than
    the input space: "is this row ours?" (j#99938, j#99950), and then "is it ours,
    asked before whether it is even self-consistent?" (j#99960). Enumerating the
    product and asserting the enumeration is complete is what stops the next such
    branch from being invisible.
    """

    def test_the_grid_covers_the_whole_declared_by_locator_product(self):
        expected = {
            (declared, locator)
            for declared in _DECLARED_KINDS
            for locator in _LOCATOR_KINDS
        }
        self.assertEqual(set(_ROW_GRID), expected)

    def test_the_grid_exercises_the_whole_disposition_vocabulary(self):
        covered = {disposition for _row, disposition in _ROW_GRID.values()}
        covered |= {disposition for _l, _r, disposition in _ROW_SHAPES}
        self.assertEqual(covered, set(ROW_DISPOSITIONS))

    def test_every_cell_lands_on_its_disposition(self):
        for cell, (row, expected) in sorted(_ROW_GRID.items()):
            with self.subTest(cell=cell):
                verdict = classify_inventory_row(row, "w1")
                self.assertEqual(verdict.disposition, expected)
                if expected == ROW_REFUSED:
                    self.assertTrue(verdict.refusal, "a refusal must say why")
                else:
                    self.assertEqual(verdict.refusal, "")
                if expected == ROW_IN_SCOPE:
                    self.assertTrue(verdict.locator)

    def test_every_non_grid_shape_lands_on_its_disposition(self):
        for label, row, expected in _ROW_SHAPES:
            with self.subTest(shape=label):
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, expected
                )

    def test_a_non_text_field_is_never_promoted_into_identity_evidence(self):
        """Review j#99971 finding_1 — ``_norm`` would have stringified each of these."""
        for raw in ([], {}, 17, True, 3.5):
            with self.subTest(raw=raw):
                verdict = classify_inventory_row(
                    {"name": "n", "pane_id": "", "workspace_id": raw}, "w1"
                )
                self.assertEqual(verdict.disposition, ROW_REFUSED)
                self.assertIn("non-text field", verdict.refusal)

    def test_every_text_field_an_in_scope_row_offers_is_shape_checked(self):
        """The defect belongs to ``_norm``, so it belongs to all of its inputs.

        Closing it only on the two fields a review named would leave the next one
        open — a stringified ``cwd`` or detected provider is the same promotion of
        a malformed value into evidence.
        """
        for key in ("workspace_id", "name", "agent", "foreground_cwd", "cwd",
                    "pane_id", "pane", "location"):
            with self.subTest(field=key):
                row = _row(A, "codex", "w1:p2", workspace_id="w1")
                row["foreground_cwd"] = "/roots/" + A
                row[key] = 17
                verdict = classify_inventory_row(row, "w1")
                self.assertEqual(verdict.disposition, ROW_REFUSED)
                self.assertIn("non-text field", verdict.refusal)

    def test_a_foreign_row_is_not_refused_over_a_field_nobody_reads(self):
        """The control for the check above (review j#99978 finding_1).

        ``name`` / ``agent`` / the cwd pair are read only once a row is ours. A row
        that has already proved it lives elsewhere is out of scope, and refusing
        the whole read over the shape of a field nobody was going to read narrows
        that boundary rather than widening the guard.
        """
        for key in ("name", "agent", "foreground_cwd", "cwd"):
            with self.subTest(field=key):
                row = {"workspace_id": "w9", "pane_id": "w9:p1", "name": "n"}
                row[key] = 17
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, ROW_OUT_OF_SCOPE
                )

    def test_a_foreign_row_whose_LOCATION_shape_is_unreadable_still_refuses(self):
        """Location is what scope is decided on, so its shape is asked first."""
        for key in ("workspace_id", "pane_id"):
            with self.subTest(field=key):
                row = {"workspace_id": "w9", "pane_id": "w9:p1", "name": "n"}
                row[key] = 17
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, ROW_REFUSED
                )

    def test_a_row_naming_two_different_panes_refuses(self):
        """The canonical reader takes the first key that answers; disagreement is
        not a preference order, it is a row that has not identified a pane."""
        verdict = classify_inventory_row(
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "pane": "w9:p9"},
            "w1",
        )
        self.assertEqual(verdict.disposition, ROW_REFUSED)
        self.assertIn("different pane locators", verdict.refusal)

    def test_a_foreign_row_naming_two_panes_refuses_too(self):
        """Location contradictions refuse wherever they are, unlike evidence ones.

        The alias check sits with the LOCATION fields on purpose: a row that cannot
        agree with itself about which pane it is has not proved it lives elsewhere
        either, so it cannot be filed as out-of-scope.
        """
        verdict = classify_inventory_row(
            {"name": encode_assigned_name(A, "codex"), "workspace_id": "w9",
             "pane_id": "w9:p1", "pane": "w8:p1"},
            "w1",
        )
        self.assertEqual(verdict.disposition, ROW_REFUSED)
        self.assertIn("different pane locators", verdict.refusal)

    def test_an_in_scope_row_may_omit_the_evidence_fields_entirely(self):
        """Absent is not malformed: the later conjuncts judge what is missing."""
        self.assertEqual(
            classify_inventory_row(
                {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
                 "workspace_id": "w1"},
                "w1",
            ).disposition,
            ROW_IN_SCOPE,
        )

    def test_both_cwd_spellings_are_shape_checked(self):
        for row in (
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "workspace_id": "w1", "foreground_cwd": 17, "cwd": "/x"},
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "workspace_id": "w1", "cwd": 17},
        ):
            with self.subTest(row=sorted(row)):
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, ROW_REFUSED
                )

    def test_locator_aliases_that_agree_are_fine(self):
        for row in (
            {"name": encode_assigned_name(A, "codex"), "pane": "w1:p2"},
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "pane": "w1:p2"},
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "pane": ""},
        ):
            with self.subTest(row=sorted(row)):
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, ROW_IN_SCOPE
                )

    def test_absent_and_null_and_blank_stay_absent(self):
        """The shape check narrows nothing that ``_norm`` already folded away."""
        for row in (
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2"},
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "workspace_id": None},
            {"name": encode_assigned_name(A, "codex"), "pane_id": "w1:p2",
             "workspace_id": "   "},
        ):
            with self.subTest(row=sorted(row)):
                self.assertEqual(
                    classify_inventory_row(row, "w1").disposition, ROW_IN_SCOPE
                )

    def test_a_contradictory_row_refuses_even_when_neither_side_is_ours(self):
        """Review j#99960 finding_1 — it used to pass as out-of-scope."""
        row, expected = _ROW_GRID[("foreign", "other_foreign")]
        self.assertEqual(expected, ROW_REFUSED)
        verdict = classify_inventory_row(row, "w1")
        self.assertIn("while its locator says", verdict.refusal)

    def test_a_refused_row_anywhere_refuses_the_whole_read(self):
        for cell, (row, expected) in sorted(_ROW_GRID.items()):
            if expected != ROW_REFUSED:
                continue
            with self.subTest(cell=cell):
                panes, refusal = coordinator_panes_in(
                    [_row(A, "codex", "w1:p2", workspace_id="w1"), row], "w1"
                )
                self.assertEqual(panes, ())
                self.assertTrue(refusal)

    def test_out_of_scope_rows_leave_the_read_intact(self):
        rows = [_row(A, "codex", "w1:p2", workspace_id="w1")]
        rows += [
            row for row, expected in _ROW_GRID.values()
            if expected == ROW_OUT_OF_SCOPE
        ]
        panes, refusal = coordinator_panes_in(rows, "w1")
        self.assertEqual(refusal, "")
        self.assertEqual([pane.locator for pane in panes], ["w1:p2"])


class CoordinatorPaneReadTest(unittest.TestCase):
    def test_an_undecodable_row_inside_the_target_workspace_refuses(self):
        rows = [
            _row(A, "codex", "w1:p2", workspace_id="w1"),
            {"name": "not-mzb1", "pane_id": "w1:p9", "workspace_id": "w1"},
        ]
        panes, refusal = coordinator_panes_in(rows, "w1")
        self.assertEqual(panes, ())
        self.assertIn("no decodable mozyo identity", refusal)

    def test_a_duplicate_locator_refuses(self):
        rows = [
            _row(A, "codex", "w1:p2", workspace_id="w1"),
            _row(A, "claude", "w1:p2", workspace_id="w1"),
        ]
        panes, refusal = coordinator_panes_in(rows, "w1")
        self.assertEqual(panes, ())
        self.assertIn("appears twice", refusal)

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


class _FakeAttestation:
    """A fake attestation port: the specification, not a patched read."""

    def __init__(self, refuse: "dict | None" = None) -> None:
        self.refuse = refuse or {}
        self.asked: list = []

    def attested(self, pane):
        self.asked.append(pane.locator)
        state = self.refuse.get(pane.locator)
        return (state is None), (state or "ok")


class _FakeLaneFacts:
    def __init__(self, facts=None, unreadable: bool = False) -> None:
        self.facts = facts or {}
        self.unreadable = unreadable
        self.reads = 0

    def lane_facts(self):
        self.reads += 1
        return None if self.unreadable else self.facts


class _FakeWorkspaces:
    def __init__(self, mapping=None) -> None:
        self.mapping = mapping or {}
        self.asked: list = []

    def workspace_of(self, cwd: str) -> str:
        self.asked.append(cwd)
        return self.mapping.get(cwd, "")


class ProjectColumnAuthorityTest(unittest.TestCase):
    """Reviews j#99885 / j#99904 / j#99913 / j#99931 — what may become a pair.

    Driven through fake ports rather than monkeypatched module reads, so the
    specification is stated in the doubles themselves (the architecture policy the
    j#99931 finding_4 carve answers).
    """

    def _authority(self, *, attestation=None, lanes=None, workspaces=None):
        self.attestation = attestation or _FakeAttestation()
        self.lanes = lanes or _FakeLaneFacts()
        self.workspaces = workspaces or _FakeWorkspaces()
        return ProjectColumnAuthority(
            attestation=self.attestation, lanes=self.lanes, workspaces=self.workspaces
        )

    def _row(self, workspace, role, locator, lane="", **over):
        row = {
            "name": encode_assigned_name(workspace, role, lane),
            "pane_id": locator,
            "agent_status": "idle",
            "agent": role,
            "workspace_id": "w1",
            "foreground_cwd": f"/roots/{workspace}",
        }
        row.update(over)
        return row

    def _resolvable(self, *workspaces):
        return _FakeWorkspaces({f"/roots/{ws}": ws for ws in workspaces})

    def test_a_well_formed_pair_of_pairs_resolves(self):
        authority = self._authority(workspaces=self._resolvable(A, B))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                self._row(B, "codex", "w1:p4"), self._row(B, "claude", "w1:p5"),
            ],
            target_workspace="w1",
        )
        self.assertTrue(decision.ok, decision.refusal)
        self.assertEqual(sorted(decision.groups), [(A, "default"), (B, "default")])
        self.assertEqual(self.lanes.reads, 0, "default lanes need no lifecycle read")

    def test_a_malformed_group_is_refused_without_touching_any_port(self):
        authority = self._authority()
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2"), self._row(A, "nethack", "w1:p3")],
            target_workspace="w1",
        )
        self.assertFalse(decision.ok)
        self.assertIn("unrecognised provider", decision.refusal)
        self.assertEqual(self.attestation.asked, [])
        self.assertEqual(self.workspaces.asked, [])
        self.assertEqual(self.lanes.reads, 0)

    def test_the_configured_top_pair_is_refused(self):
        authority = self._authority()
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3")],
            target_workspace="w1",
            top_workspace_id=A,
        )
        self.assertFalse(decision.ok)
        self.assertIn("configured top coordinator", decision.refusal)

    def test_an_undecodable_row_in_the_target_workspace_refuses_the_whole_set(self):
        """Review j#99931 finding_2 — skipping it cost six pane moves."""
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                {"name": "not-a-mzb1-name", "pane_id": "w1:p9"},
            ],
            target_workspace="w1",
        )
        self.assertFalse(decision.ok)
        self.assertIn("no decodable mozyo identity", decision.refusal)

    def test_a_row_in_another_workspace_is_out_of_scope_not_a_refusal(self):
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                {"name": "not-a-mzb1-name", "pane_id": "w2:p9"},
            ],
            target_workspace="w1",
        )
        self.assertTrue(decision.ok, decision.refusal)

    def test_a_duplicate_locator_refuses(self):
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p2")],
            target_workspace="w1",
        )
        self.assertFalse(decision.ok)
        self.assertIn("appears twice", decision.refusal)

    def test_a_named_lane_needs_both_kind_and_active_disposition(self):
        """Review j#99931 finding_3 — the kind alone let a hibernated lane through."""
        rows = [
            self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
            self._row(A, "codex", "w1:p4", lane="impl-1"),
        ]
        for fact, fragment in (
            (None, "no durable lane-kind"),
            (LaneFact(kind=LANE_KIND_IMPLEMENTATION, disposition=DISPOSITION_ACTIVE),
             "not 'delegated_coordinator'"),
            (LaneFact(kind=LANE_KIND_DELEGATED_COORDINATOR, disposition="hibernated"),
             "not 'active'"),
        ):
            with self.subTest(fact=fact):
                facts = {} if fact is None else {(A, "impl-1"): fact}
                authority = self._authority(
                    lanes=_FakeLaneFacts(facts), workspaces=self._resolvable(A)
                )
                decision = authority.resolve(rows, target_workspace="w1")
                self.assertFalse(decision.ok)
                self.assertIn(fragment, decision.refusal)

    def test_an_unreadable_lane_authority_refuses_rather_than_defaults(self):
        authority = self._authority(
            lanes=_FakeLaneFacts(unreadable=True), workspaces=self._resolvable(A)
        )
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2", lane="impl-1")], target_workspace="w1"
        )
        self.assertFalse(decision.ok)
        self.assertIn("unreadable", decision.refusal)

    def test_own_panes_are_exempt_from_the_two_facts_they_cannot_answer(self):
        """...and from nothing else (review j#99931 finding_1)."""
        own = OwnSlot(
            locator="w1:p4",
            assigned_name=encode_assigned_name(B, "codex", "delegated-1"),
            provider="codex",
        )
        authority = self._authority(
            attestation=_FakeAttestation({"w1:p4": "absent"}),
            workspaces=self._resolvable(A, B),
        )
        rows = [
            self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
            self._row(B, "codex", "w1:p4", lane="delegated-1"),
        ]
        decision = authority.resolve(
            rows, target_workspace="w1", own_slots=[own]
        )
        self.assertTrue(decision.ok, decision.refusal)
        self.assertNotIn("w1:p4", self.attestation.asked)
        self.assertIn("/roots/" + B, self.workspaces.asked)

    def test_an_own_pane_still_answers_what_its_row_already_says(self):
        own = OwnSlot(
            locator="w1:p4", assigned_name=encode_assigned_name(B, "codex"),
            provider="codex",
        )
        for over, fragment in (
            ({"agent": ""}, "shell residue"),
            ({"agent": "claude"}, "while its assigned name claims"),
            ({"foreground_cwd": "/roots/" + A}, "while its assigned name claims"),
        ):
            with self.subTest(over=over):
                authority = self._authority(workspaces=self._resolvable(A, B))
                rows = [
                    self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                    self._row(B, "codex", "w1:p4", **over),
                ]
                decision = authority.resolve(
                    rows, target_workspace="w1", own_slots=[own]
                )
                self.assertFalse(decision.ok)
                self.assertIn(fragment, decision.refusal)

    def test_the_own_exemption_is_bound_to_an_exact_identity_join(self):
        own = OwnSlot(
            locator="w1:p4", assigned_name=encode_assigned_name(B, "claude"),
            provider="claude",
        )
        authority = self._authority(workspaces=self._resolvable(A, B))
        rows = [
            self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
            self._row(B, "codex", "w1:p4"),
        ]
        decision = authority.resolve(rows, target_workspace="w1", own_slots=[own])
        self.assertFalse(decision.ok)
        self.assertIn("an identity this run did not launch there", decision.refusal)

    def test_a_launched_locator_absent_from_the_workspace_refuses(self):
        own = OwnSlot(
            locator="w1:pGHOST", assigned_name=encode_assigned_name(B, "codex"),
            provider="codex",
        )
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2")], target_workspace="w1", own_slots=[own]
        )
        self.assertFalse(decision.ok)
        self.assertIn("inventory does not hold it", decision.refusal)

    def test_the_decision_carries_the_resolved_own_key(self):
        """One source for "which pair is ours" (review j#99938 finding_2)."""
        own = OwnSlot(
            locator="w1:p4", assigned_name=encode_assigned_name(B, "codex"),
            provider="codex",
        )
        authority = self._authority(workspaces=self._resolvable(A, B))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                self._row(B, "codex", "w1:p4"),
            ],
            target_workspace="w1",
            own_slots=[own],
            expected_own_key=(B, "default"),
        )
        self.assertTrue(decision.ok, decision.refusal)
        self.assertEqual(decision.own_key, (B, "default"))

    def test_a_run_claiming_a_pair_its_panes_do_not_decode_to_is_refused(self):
        own = OwnSlot(
            locator="w1:p4", assigned_name=encode_assigned_name(B, "codex"),
            provider="codex",
        )
        authority = self._authority(workspaces=self._resolvable(A, B))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"), self._row(A, "claude", "w1:p3"),
                self._row(B, "codex", "w1:p4"),
            ],
            target_workspace="w1",
            own_slots=[own],
            expected_own_key=(C, "default"),
        )
        self.assertFalse(decision.ok)
        self.assertIn("the workspace does not corroborate", decision.refusal)

    def test_a_row_claiming_the_target_workspace_without_a_locator_refuses(self):
        """Review j#99938 finding_1 — scope itself is a conjunct."""
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"),
                {"name": "not-mzb1", "pane_id": "", "workspace_id": "w1"},
            ],
            target_workspace="w1",
        )
        self.assertFalse(decision.ok)
        self.assertIn("carries no pane locator", decision.refusal)

    def test_a_row_whose_declared_workspace_contradicts_its_locator_refuses(self):
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [
                self._row(A, "codex", "w1:p2"),
                self._row(A, "claude", "w9:p3", workspace_id="w1"),
            ],
            target_workspace="w1",
        )
        self.assertFalse(decision.ok)
        self.assertIn("while its locator says", decision.refusal)

    def test_a_group_of_one_live_provider_is_accepted_when_every_authority_resolves(self):
        """The disputed half of finding_3 (verdict j#99888 / Answer j#99900)."""
        authority = self._authority(workspaces=self._resolvable(A))
        decision = authority.resolve(
            [self._row(A, "codex", "w1:p2")], target_workspace="w1"
        )
        self.assertTrue(decision.ok, decision.refusal)
        self.assertEqual(sorted(decision.groups), [(A, "default")])


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
