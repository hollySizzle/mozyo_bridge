"""Redmine #14996 R2 — an appended coordinator pair owns its own project column.

The live finding (j#99833): a second project's coordinator pair launched into the
shared ``project-coordinators`` workspace produced an L — the first project's
Claude spanning the whole bottom row while the new pair sat stacked in the top
right — instead of two full-height columns. Routing and identity were correct;
only the geometry was wrong, and it defeats the one-screen overview the mode
exists for.

These tests drive the real nested split tree (:mod:`tests.support.herdr_pane_tree`)
rather than an argv assertion, because the defect is only expressible in a nested
tree: the previous suite checked the ``--split`` argv and passed while the screen
was an L (j#99845).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.herdr_pane_tree import PaneTreeHerdr  # noqa: E402

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E402,E501
    COLUMN_APPLIED,
    COLUMN_FAILED,
    COLUMN_MATCHED,
    COLUMN_NOT_APPLICABLE,
    reflow_project_columns,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E402,E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

TOP = "ws-top"
PROJECT_A = "ws-server-management"
PROJECT_B = "ws-gk-3500"
PROJECT_C = "ws-gk-2045"


def _name(workspace: str, role: str) -> str:
    return encode_assigned_name(workspace, role)


def _result(workspace_id: str, launched: "list[str]") -> SessionStartResult:
    result = SessionStartResult(workspace_id=workspace_id, lane_id="default")
    for provider, locator in zip(("codex", "claude"), launched):
        result.slots.append(
            SlotResult(
                provider=provider,
                assigned_name=_name(workspace_id, provider),
                outcome=SLOT_LAUNCHED,
                locator=locator,
            )
        )
    return result


def _seed_first_project(herdr: PaneTreeHerdr, workspace: str):
    """One project's coordinator pair, as a fresh launch leaves it: ``down`` split."""
    tab = herdr.new_tab()
    base = herdr.seed_pane(tab)
    top = herdr.split_pane(tab, base, "right", _name(workspace, "codex"), focus=True)
    bottom = herdr.split_pane(tab, top, "down", _name(workspace, "claude"))
    tab.remove(base)  # the #13330 root reclaim
    return tab, top, bottom


def _append_pair_as_today(herdr: PaneTreeHerdr, tab, workspace: str):
    """Launch a pair exactly as the pre-fix rail does: split the ACTIVE pane."""
    first = herdr.launch_into(tab, _name(workspace, "codex"), focus=True)
    second = herdr.launch_into(tab, _name(workspace, "claude"), split="down")
    return first, second


def _run(herdr: PaneTreeHerdr, result, workspace, **overrides):
    # The resolved shared herdr workspace the run landed in — session-start records it
    # on the result before the geometry step, and the reflow reads it from there.
    result.herdr_workspace_id = herdr.workspace_id
    kwargs = {
        "project_coordinator": True,
        "launched": 2,
        "initial_occupancy": 0,
        "dry_run": False,
        "binary": "herdr",
        "runner": herdr,
        "timeout": 5.0,
        "env": None,
    }
    kwargs.update(overrides)
    return reflow_project_columns(result, **kwargs)


class ProjectColumnGeometryTest(unittest.TestCase):
    def _columns(self, herdr: PaneTreeHerdr, panes: "dict[str, list[str]]") -> dict:
        """``{project: (x, width)}`` after asserting each pair is one full column."""
        rects = herdr.rects()
        heights = {y + h for _x, y, _w, h in rects.values()}
        tops = {y for _x, y, _w, _h in rects.values()}
        columns = {}
        for project, members in panes.items():
            xs = {rects[pane][0] for pane in members}
            widths = {rects[pane][2] for pane in members}
            self.assertEqual(len(xs), 1, f"{project} panes are not vertically aligned")
            self.assertEqual(len(widths), 1, f"{project} panes have different widths")
            stacked = sorted(members, key=lambda pane: rects[pane][1])
            self.assertEqual(rects[stacked[0]][1], min(tops))
            self.assertEqual(
                rects[stacked[-1]][1] + rects[stacked[-1]][3], max(heights)
            )
            columns[project] = (xs.pop(), widths.pop())
        return columns

    def test_appended_pair_becomes_its_own_full_height_column(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)

        # The defect, reproduced: project A's lower pane spans the whole tab width
        # while the appended pair is nested inside project A's upper half.
        before = herdr.rects()
        self.assertEqual(before[a_bottom][2], 54)
        self.assertEqual(before[a_top][2], 27)
        self.assertGreater(before[b_top][0], before[a_top][0])

        outcome, detail = _run(herdr, _result(PROJECT_B, [b_top, b_bottom]), PROJECT_B)
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        columns = self._columns(
            herdr, {PROJECT_A: [a_top, a_bottom], PROJECT_B: [b_top, b_bottom]}
        )
        # Two equal columns that tile the tab: the even 2x2 the owner accepted.
        self.assertEqual(columns[PROJECT_A], (0, 27))
        self.assertEqual(columns[PROJECT_B], (27, 27))
        # Codex above Claude within each column, unchanged by the reflow.
        rects = herdr.rects()
        self.assertLess(rects[a_top][1], rects[a_bottom][1])
        self.assertLess(rects[b_top][1], rects[b_bottom][1])
        # Every pane is back in the one shared tab; no temp tab survived.
        self.assertEqual(len(herdr.tabs), 1)
        self.assertEqual(sorted(tab.panes()), sorted([a_top, a_bottom, b_top, b_bottom]))

    def test_result_is_independent_of_which_pane_was_active_before_launch(self):
        """j#99845: the geometry must not depend on pre-launch focus."""
        geometries = []
        for focus_bottom in (False, True):
            herdr = PaneTreeHerdr()
            tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
            if focus_bottom:
                tab.focused = a_bottom
            b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)
            outcome, detail = _run(
                herdr, _result(PROJECT_B, [b_top, b_bottom]), PROJECT_B
            )
            self.assertEqual(outcome, COLUMN_APPLIED, detail)
            geometries.append(
                self._columns(
                    herdr, {PROJECT_A: [a_top, a_bottom], PROJECT_B: [b_top, b_bottom]}
                )
            )
        self.assertEqual(geometries[0], geometries[1])

    def test_third_pair_gets_its_own_column_without_crossing_the_others(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)
        self.assertEqual(
            _run(herdr, _result(PROJECT_B, [b_top, b_bottom]), PROJECT_B)[0],
            COLUMN_APPLIED,
        )
        c_top, c_bottom = _append_pair_as_today(herdr, tab, PROJECT_C)
        outcome, detail = _run(herdr, _result(PROJECT_C, [c_top, c_bottom]), PROJECT_C)
        self.assertEqual(outcome, COLUMN_APPLIED, detail)

        columns = self._columns(
            herdr,
            {
                PROJECT_A: [a_top, a_bottom],
                PROJECT_B: [b_top, b_bottom],
                PROJECT_C: [c_top, c_bottom],
            },
        )
        spans = sorted(columns.values())
        for (x, width), (next_x, _next_width) in zip(spans, spans[1:]):
            self.assertEqual(x + width, next_x, "project columns overlap or leave a gap")
        # The newest project is appended on the right; existing columns keep their order.
        self.assertEqual(max(columns, key=lambda key: columns[key][0]), PROJECT_C)
        self.assertLess(columns[PROJECT_A][0], columns[PROJECT_B][0])

    def test_already_columnar_tab_moves_no_pane(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)
        self.assertEqual(
            _run(herdr, _result(PROJECT_B, [b_top, b_bottom]), PROJECT_B)[0],
            COLUMN_APPLIED,
        )
        moves_before = sum(1 for call in herdr.calls if call[:2] == ["pane", "move"])
        outcome, _detail = _run(herdr, _result(PROJECT_B, [b_top, b_bottom]), PROJECT_B)
        self.assertEqual(outcome, COLUMN_MATCHED)
        moves_after = sum(1 for call in herdr.calls if call[:2] == ["pane", "move"])
        self.assertEqual(moves_before, moves_after)


class ProjectColumnRestraintTest(unittest.TestCase):
    """The reflow must never fire outside the one case it was authorised for."""

    def _shared_tab(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)
        return herdr, (b_top, b_bottom)

    def _assert_untouched(self, herdr, outcome, expected=COLUMN_NOT_APPLICABLE):
        self.assertEqual(outcome, expected)
        self.assertEqual([call for call in herdr.calls if call[:2] == ["pane", "move"]], [])

    def test_non_role_grouped_placement_reads_nothing(self):
        herdr, launched = self._shared_tab()
        outcome, _detail = _run(
            herdr, _result(PROJECT_B, list(launched)), PROJECT_B, project_coordinator=False
        )
        self._assert_untouched(herdr, outcome)
        self.assertEqual(herdr.calls, [])

    def test_dry_run_reads_nothing(self):
        herdr, launched = self._shared_tab()
        outcome, _detail = _run(
            herdr, _result(PROJECT_B, list(launched)), PROJECT_B, dry_run=True
        )
        self._assert_untouched(herdr, outcome)
        self.assertEqual(herdr.calls, [])

    def test_adopt_only_run_moves_nothing(self):
        herdr, launched = self._shared_tab()
        result = SessionStartResult(workspace_id=PROJECT_B, lane_id="default")
        result.slots.append(
            SlotResult(
                provider="codex",
                assigned_name=_name(PROJECT_B, "codex"),
                outcome=SLOT_ADOPTED,
                locator=launched[0],
            )
        )
        outcome, _detail = _run(herdr, result, PROJECT_B, launched=0)
        self._assert_untouched(herdr, outcome)

    def test_single_provider_heal_beside_a_live_sibling_moves_nothing(self):
        herdr, launched = self._shared_tab()
        outcome, _detail = _run(
            herdr,
            _result(PROJECT_B, list(launched)),
            PROJECT_B,
            launched=1,
            initial_occupancy=1,
        )
        self._assert_untouched(herdr, outcome)

    def test_first_project_in_the_shared_workspace_moves_nothing(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        outcome, _detail = _run(herdr, _result(PROJECT_A, [a_top, a_bottom]), PROJECT_A)
        self._assert_untouched(herdr, outcome)

    def test_top_coordinator_in_its_own_workspace_is_untouched(self):
        """The top pair lives alone in its dedicated workspace: nothing to append to."""
        herdr = PaneTreeHerdr(workspace_id="w9")
        tab, top, bottom = _seed_first_project(herdr, TOP)
        outcome, _detail = _run(herdr, _result(TOP, [top, bottom]), TOP)
        self._assert_untouched(herdr, outcome)


class ProjectColumnFailClosedTest(unittest.TestCase):
    """A reflow that could not be established is never reported as a success."""

    def _scenario(self):
        herdr = PaneTreeHerdr()
        tab, a_top, a_bottom = _seed_first_project(herdr, PROJECT_A)
        b_top, b_bottom = _append_pair_as_today(herdr, tab, PROJECT_B)
        return herdr, tab, (a_top, a_bottom), (b_top, b_bottom)

    def test_refused_detach_is_typed_and_keeps_every_pane_in_the_shared_tab(self):
        herdr, tab, project_a, project_b = self._scenario()
        herdr.move_refusals.add(project_a[1])  # the anchor's lower pane will not move
        result = _result(PROJECT_B, list(project_b))
        outcome, detail = _run(herdr, result, PROJECT_B)
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("refused", detail)
        self.assertIn("returned to the shared tab", detail)
        self.assertEqual(len(herdr.tabs), 1)
        self.assertEqual(
            sorted(tab.panes()), sorted(list(project_a) + list(project_b))
        )
        result.column_outcome, result.column_detail = outcome, detail
        self.assertFalse(result.column_ok)

    def test_a_pane_left_outside_the_shared_tab_is_named_in_the_detail(self):
        herdr, tab, _project_a, project_b = self._scenario()
        # The first detach lands, the second is refused, and so is the recovery move:
        # one pane is genuinely stranded in a temp tab and must be reported as such.
        herdr.refuse_from_move = 2
        outcome, detail = _run(herdr, _result(PROJECT_B, list(project_b)), PROJECT_B)
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("NOT in the shared", detail)
        self.assertIn("live-relayout runbook", detail)
        self.assertIn(project_b[1], detail)
        self.assertNotIn(project_b[1], tab.panes())

    def test_same_tab_no_op_move_is_not_read_as_a_completed_move(self):
        herdr, _tab, _project_a, project_b = self._scenario()
        herdr.move_unchanged.add(project_b[1])
        outcome, detail = _run(herdr, _result(PROJECT_B, list(project_b)), PROJECT_B)
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("no completed move", detail)

    def test_identity_change_across_the_reflow_is_not_claimed_as_geometry(self):
        herdr, _tab, project_a, project_b = self._scenario()
        # A rename after the last move: the layout is right, the identity is not.
        herdr.rename_after_moves[6] = (project_a[0], _name("ws-impostor", "codex"))
        outcome, detail = _run(herdr, _result(PROJECT_B, list(project_b)), PROJECT_B)
        self.assertEqual(outcome, COLUMN_FAILED)
        self.assertIn("inventory changed across the reflow", detail)

    def test_a_failed_column_keeps_the_run_from_reporting_success(self):
        result = SessionStartResult(workspace_id=PROJECT_B, lane_id="default")
        result.column_outcome = COLUMN_FAILED
        self.assertFalse(result.column_ok)
        self.assertFalse(result.ok)
        self.assertEqual(result.as_payload()["column_outcome"], COLUMN_FAILED)

    def test_an_unrecognised_column_token_is_not_a_success(self):
        result = SessionStartResult(workspace_id=PROJECT_B, lane_id="default")
        result.column_outcome = "appllied"
        self.assertFalse(result.column_ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
