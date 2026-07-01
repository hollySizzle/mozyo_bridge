"""Fake-port / pure specifications for the group-window action boundary (#12982).

These exercise the ``cockpit_group_window_command`` pure decision and use case
directly with a synthetic :class:`CockpitGroupWindowOps` — no real tmux server
and no live multi-window discovery. They pin:

- the pure ``resolve_group_window_action`` routing: cross-window focus priority,
  group-marker (never name) location, the ungrouped-Unit "always its own window"
  rule, the different-lane "not a duplicate" rule, and the stale-window "no codex
  column to append beside" block;
- the ``CockpitGroupWindowUseCase`` walk: it reads managed windows through the
  port and threads the port's ``rightmost_codex_anchor`` into the decision;
- the ``GROUP_ACTION_*`` vocabulary as the single source of truth (re-exported by
  ``commands``).

The end-to-end behavior over the live ``commands._read_managed_cockpit_windows``
seam stays pinned by the cockpit group-window characterization tests; this file
pins the boundary in isolation.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.application import commands
from mozyo_bridge.application.cockpit_group_window_command import (
    GROUP_ACTION_APPEND,
    GROUP_ACTION_CREATE,
    GROUP_ACTION_FOCUS,
    GROUP_ACTIONS,
    CockpitGroupWindowUseCase,
    resolve_group_window_action,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    CockpitWorkspace,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    GroupWindowDecision,
    PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
)


def _faithful_decision(group_id="alpha", window="Alpha"):
    return GroupWindowDecision(
        presentation_mode=PROJECT_GROUP_PRESENTATION_TMUX_WINDOW,
        desired_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
        executed_surface=GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
        group_id=group_id,
        label=window,
        desired_window_name=window,
        degraded=False,
    )


def _codex_col(pane_id, ws_id, *, lane="default", left=0, width=80):
    return {
        "pane_id": pane_id, "workspace_id": ws_id, "role": "codex",
        "lane_id": lane, "pane_left": left, "pane_width": width,
    }


def _rightmost(codex_columns):
    """A deterministic stand-in for ``commands._rightmost_codex_anchor``."""

    if not codex_columns:
        return None
    return max(
        codex_columns,
        key=lambda c: (c.get("pane_left") or 0, c.get("pane_id") or ""),
    ).get("pane_id")


class _FakeGroupWindowOps:
    """A synthetic :class:`CockpitGroupWindowOps` recording its seam calls."""

    def __init__(self, managed, *, anchor=_rightmost):
        self._managed = managed
        self._anchor = anchor
        self.read_calls: list[str] = []
        self.anchor_calls: list = []

    def read_managed_windows(self, session):
        self.read_calls.append(session)
        return self._managed

    def rightmost_codex_anchor(self, codex_columns):
        self.anchor_calls.append(codex_columns)
        return self._anchor(codex_columns)


def _resolve(managed, *, group_id="alpha", ws_id="wsA", lane="default", anchor=_rightmost):
    ws = CockpitWorkspace(ws_id, "repoA", "/repoA", lane_id=lane)
    return resolve_group_window_action(
        ws,
        "mozyo-cockpit",
        decision=_faithful_decision(group_id=group_id),
        codex_ratio=70,
        launch=lambda role, w: f"{role}-cmd",
        managed=managed,
        rightmost_codex_anchor=anchor,
    )


class VocabularyTest(unittest.TestCase):
    def test_constants_are_the_source_commands_re_exports(self) -> None:
        # `commands` re-exports these names; the boundary module owns them.
        self.assertEqual("group_focus", GROUP_ACTION_FOCUS)
        self.assertIs(commands.GROUP_ACTION_FOCUS, GROUP_ACTION_FOCUS)
        self.assertIs(commands.GROUP_ACTIONS, GROUP_ACTIONS)
        self.assertEqual(
            (GROUP_ACTION_FOCUS, GROUP_ACTION_APPEND, GROUP_ACTION_CREATE),
            GROUP_ACTIONS,
        )


class ResolveGroupWindowActionTest(unittest.TestCase):
    def test_cross_window_focus_when_unit_already_placed_anywhere(self) -> None:
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [_codex_col("%5", "wsA")]},
        ]
        action, plan, blocked, window = _resolve(managed)
        self.assertEqual(GROUP_ACTION_FOCUS, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        # Focuses the exact existing pane, in whatever window holds it.
        self.assertEqual(("select-window", "-t", "%5"), plan.commands[0].argv)

    def test_append_into_existing_group_window_by_marker(self) -> None:
        # A DIFFERENT unit occupies the alpha window; the new unit appends beside
        # its rightmost codex pane.
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [_codex_col("%9", "wsZ")]},
        ]
        action, plan, blocked, window = _resolve(managed)
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        self.assertEqual("split-window", plan.commands[0].argv[0])
        self.assertIn("%9", plan.commands[0].argv)

    def test_create_new_group_window_when_no_matching_window(self) -> None:
        managed = [
            {"window": "cockpit", "group_id": "",
             "columns": [_codex_col("%1", "wsZ")]},
        ]
        action, plan, blocked, window = _resolve(managed)
        self.assertEqual(GROUP_ACTION_CREATE, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        self.assertEqual("new-window", plan.commands[0].argv[0])

    def test_ungrouped_unit_never_shares_a_window(self) -> None:
        managed = [
            {"window": "cockpit", "group_id": "",
             "columns": [_codex_col("%1", "wsZ")]},
        ]
        action, _plan, _blocked, _window = _resolve(managed, group_id=None)
        self.assertEqual(GROUP_ACTION_CREATE, action)

    def test_colliding_window_names_route_by_group_marker_not_name(self) -> None:
        # #12330 review j#62380: two windows share the display name "ab" but carry
        # distinct group markers. A unit in group "beta" must append into the beta
        # window (by marker), never the alpha window.
        managed = [
            {"window_id": "@1", "window": "ab", "group_id": "alpha",
             "columns": [_codex_col("%5", "wsA")]},
            {"window_id": "@2", "window": "ab", "group_id": "beta",
             "columns": [_codex_col("%6", "wsB")]},
        ]
        action, plan, blocked, _window = _resolve(managed, group_id="beta", ws_id="wsNew")
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIsNone(blocked)
        self.assertIn("%6", plan.commands[0].argv)
        self.assertNotIn("%5", plan.commands[0].argv)

    def test_different_lane_is_not_a_duplicate(self) -> None:
        # Same workspace id, DIFFERENT lane -> not a focus; appends as its own
        # column (worktree / clone semantics, #11820).
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [_codex_col("%9", "wsA", lane="worktree-x")]},
        ]
        action, _plan, _blocked, _window = _resolve(managed, lane="default")
        self.assertEqual(GROUP_ACTION_APPEND, action)

    def test_stale_group_window_without_codex_is_blocked(self) -> None:
        # The group window exists (by marker) but its only column is a non-codex
        # pane, so there is nothing to anchor an append beside -> blocked.
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [{"pane_id": "%7", "workspace_id": "wsZ", "role": "claude",
                          "lane_id": "default", "pane_left": 0, "pane_width": 80}]},
        ]
        action, plan, blocked, window = _resolve(managed, ws_id="wsNew")
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIsNone(plan)
        self.assertIn("no mozyo-identified codex column", blocked)
        self.assertEqual("Alpha", window)

    def test_rightmost_anchor_picks_the_visually_rightmost_codex_pane(self) -> None:
        # Two codex panes in the group window; the append anchors on the rightmost
        # by geometry, threaded through the injected anchor.
        managed = [
            {"window": "Alpha", "group_id": "alpha", "columns": [
                _codex_col("%a", "wsZ", left=0, width=40),
                _codex_col("%b", "wsY", left=40, width=40),
            ]},
        ]
        action, plan, _blocked, _window = _resolve(managed, ws_id="wsNew")
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIn("%b", plan.commands[0].argv)


class UseCaseTest(unittest.TestCase):
    def test_reads_managed_windows_and_threads_the_anchor(self) -> None:
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [_codex_col("%9", "wsZ")]},
        ]
        ops = _FakeGroupWindowOps(managed)
        ws = CockpitWorkspace("wsNew", "repoA", "/repoA", lane_id="default")
        action, plan, blocked, window = CockpitGroupWindowUseCase(ops).resolve(
            ws,
            "mozyo-cockpit",
            decision=_faithful_decision(),
            codex_ratio=70,
            launch=lambda role, w: f"{role}-cmd",
        )
        self.assertEqual(GROUP_ACTION_APPEND, action)
        self.assertIsNone(blocked)
        self.assertEqual("Alpha", window)
        self.assertIn("%9", plan.commands[0].argv)
        # The managed-window discovery was read through the port for this session,
        # and the append consulted the port's rightmost-codex-anchor seam.
        self.assertEqual(["mozyo-cockpit"], ops.read_calls)
        self.assertEqual(1, len(ops.anchor_calls))

    def test_focus_short_circuits_before_the_anchor_seam(self) -> None:
        # A cross-window duplicate resolves to focus without ever needing the
        # rightmost-codex-anchor pick.
        managed = [
            {"window": "Alpha", "group_id": "alpha",
             "columns": [_codex_col("%5", "wsA")]},
        ]
        ops = _FakeGroupWindowOps(managed)
        ws = CockpitWorkspace("wsA", "repoA", "/repoA", lane_id="default")
        action, _plan, _blocked, _window = CockpitGroupWindowUseCase(ops).resolve(
            ws,
            "mozyo-cockpit",
            decision=_faithful_decision(),
            codex_ratio=70,
            launch=lambda role, w: f"{role}-cmd",
        )
        self.assertEqual(GROUP_ACTION_FOCUS, action)
        self.assertEqual([], ops.anchor_calls)


if __name__ == "__main__":
    unittest.main()
