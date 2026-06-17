"""Cockpit column width rebalance — preview/confirm fair-share restore (Redmine #12135).

`mozyo cockpit rebalance` restores an existing live cockpit's skewed column widths
toward an EQUAL fair-share width, driving the resize off the tmux
``window_layout`` tree's top-level cells (the source of truth for which
boundaries are resizable). These tests pin the pure layout parser
(:func:`parse_window_layout` / :func:`top_level_columns`), the planner
(:func:`build_cockpit_rebalance_plan` / :func:`fair_share_widths`), the fail-fast
executor, and the preview-first / confirm-gated CLI wiring — all hermetic (no live
tmux). Load-bearing guarantees: the plan touches column *width* only (no
`set-option`, so identity pane options stay put; no `select-layout`, so the
Codex/Claude vertical splits are not flattened); it conserves the total content
width; and it FAILS CLOSED on a structurally drifted (nested 2x2) cell
rather than corrupting the layout. Synthetic, neutral identifiers only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_layout import (
    COCKPIT_WINDOW,
    build_cockpit_rebalance_plan,
    fair_share_widths,
    parse_window_layout,
    top_level_columns,
)


def _clean_column(*, width, x, codex, claude):
    """A clean column: codex (full width, h39) over claude (full width, h17)."""
    return (
        f"{width}x57,{x},0[{width}x39,{x},0,{codex},"
        f"{width}x17,{x},40,{claude}]"
    )


def _clean_skew_layout():
    """5 clean columns of widths 44 / 40 / 78 / 56 / 55 (the live US smoke skew)."""
    widths = [44, 40, 78, 56, 55]
    panes = [(1104, 1106), (953, 954), (1093, 1094), (1109, 1110), (1111, 1112)]
    cells = []
    x = 0
    for w, (codex, claude) in zip(widths, panes):
        cells.append(_clean_column(width=w, x=x, codex=codex, claude=claude))
        x += w + 1
    return "abcd,277x57,0,0{" + ",".join(cells) + "}"


def _degenerate_layout():
    """The live nested 2x2 drift: cell A is a 2x2 grid of two Units (#12136 scope).

    Top-level split has 4 cells; cell A is a vertical split whose children are
    horizontal sub-splits (two codex panes side by side over two claude panes),
    so its first leaf (%1104, width 29) does not span cell A's 85 width.
    """
    cell_a = (
        "85x57,0,0[85x39,0,0{29x39,0,0,1104,55x39,30,0,953},"
        "85x17,0,40{44x17,0,40,1106,40x17,45,40,954}]"
    )
    cell_b = _clean_column(width=55, x=86, codex=1093, claude=1094)
    cell_c = _clean_column(width=54, x=142, codex=1109, claude=1110)
    cell_d = _clean_column(width=80, x=197, codex=1111, claude=1112)
    return f"a3cd,277x57,0,0{{{cell_a},{cell_b},{cell_c},{cell_d}}}"


def _columns(layout):
    return top_level_columns(parse_window_layout(layout))


class FairShareWidthsTest(unittest.TestCase):
    def test_even_split_is_equal(self) -> None:
        self.assertEqual([20, 20, 20, 20], fair_share_widths(80, 4))

    def test_remainder_goes_to_leftmost_columns(self) -> None:
        self.assertEqual([55, 55, 55, 54, 54], fair_share_widths(273, 5))

    def test_sum_is_conserved(self) -> None:
        self.assertEqual(273, sum(fair_share_widths(273, 5)))

    def test_single_column_takes_all(self) -> None:
        self.assertEqual([99], fair_share_widths(99, 1))


class ParseWindowLayoutTest(unittest.TestCase):
    def test_strips_checksum_and_parses_clean_columns(self) -> None:
        cols = _columns(_clean_skew_layout())
        self.assertEqual(5, len(cols))
        self.assertEqual([44, 40, 78, 56, 55], [c.width for c in cols])
        self.assertEqual("%1104", cols[0].target_pane)
        self.assertTrue(all(c.clean for c in cols))

    def test_clean_column_pane_ids_are_prefixed(self) -> None:
        cols = _columns(_clean_skew_layout())
        self.assertEqual(("%1104", "%1106"), cols[0].pane_ids)

    def test_degenerate_cell_is_not_clean(self) -> None:
        cols = _columns(_degenerate_layout())
        # Top-level split has 4 cells; the first (cell A) is the 2x2 drift.
        self.assertEqual(4, len(cols))
        self.assertFalse(cols[0].clean)  # first leaf %1104 (29) < cell A (85)
        self.assertTrue(all(c.clean for c in cols[1:]))

    def test_single_column_window_is_one_clean_column(self) -> None:
        cols = _columns("9f3e,80x57,0,0[80x39,0,0,1111,80x17,0,40,1112]")
        self.assertEqual(1, len(cols))
        self.assertTrue(cols[0].clean)
        self.assertEqual("%1111", cols[0].target_pane)

    def test_malformed_layout_is_none(self) -> None:
        self.assertIsNone(parse_window_layout("not-a-layout"))
        self.assertIsNone(parse_window_layout(""))
        self.assertEqual((), top_level_columns(None))


class BuildRebalancePlanTest(unittest.TestCase):
    def _plan(self, layout):
        return build_cockpit_rebalance_plan(_columns(layout), session="mozyo-cockpit")

    def test_skew_is_not_balanced(self) -> None:
        plan = self._plan(_clean_skew_layout())
        self.assertFalse(plan.balanced)
        self.assertFalse(plan.drift)
        self.assertEqual(5, plan.column_count)
        self.assertEqual(273, plan.total_content_width)

    def test_targets_are_equal_fair_share(self) -> None:
        plan = self._plan(_clean_skew_layout())
        self.assertEqual(
            [55, 55, 55, 54, 54], [c.target_width for c in plan.columns]
        )
        # Total conserved (borders untouched) -> the window does not resize.
        self.assertEqual(
            plan.total_content_width, sum(c.target_width for c in plan.columns)
        )

    def test_resizes_every_column_except_the_last(self) -> None:
        plan = self._plan(_clean_skew_layout())
        self.assertEqual(
            [("resize-pane", "-t", "%1104", "-x", "55"),
             ("resize-pane", "-t", "%953", "-x", "55"),
             ("resize-pane", "-t", "%1093", "-x", "55"),
             ("resize-pane", "-t", "%1109", "-x", "54")],
            [c.argv for c in plan.commands],
        )

    def test_never_touches_identity_or_flattens_splits(self) -> None:
        plan = self._plan(_clean_skew_layout())
        verbs = {c.argv[0] for c in plan.commands}
        self.assertEqual({"resize-pane"}, verbs)
        self.assertNotIn("set-option", verbs)
        self.assertNotIn("select-layout", verbs)

    def test_structural_drift_fails_closed_with_no_commands(self) -> None:
        plan = self._plan(_degenerate_layout())
        self.assertTrue(plan.drift)
        self.assertFalse(plan.balanced)
        self.assertEqual((), plan.commands)
        self.assertIn("reconcile", plan.blocked_reason)
        self.assertIn("#12136", plan.blocked_reason)

    def test_already_equal_is_balanced_no_op(self) -> None:
        layout = (
            "abcd,81x57,0,0{"
            + _clean_column(width=40, x=0, codex=10, claude=11)
            + ","
            + _clean_column(width=40, x=41, codex=20, claude=21)
            + "}"
        )
        plan = build_cockpit_rebalance_plan(_columns(layout))
        self.assertTrue(plan.balanced)
        self.assertEqual((), plan.commands)

    def test_one_cell_rounding_is_within_tolerance(self) -> None:
        # 81 / 2 = 40 r1 -> targets 41/40; a 40/41 layout is within tolerance.
        layout = (
            "abcd,82x57,0,0{"
            + _clean_column(width=40, x=0, codex=10, claude=11)
            + ","
            + _clean_column(width=41, x=41, codex=20, claude=21)
            + "}"
        )
        plan = build_cockpit_rebalance_plan(_columns(layout))
        self.assertTrue(plan.balanced)
        self.assertEqual((), plan.commands)

    def test_single_column_is_always_balanced(self) -> None:
        plan = build_cockpit_rebalance_plan(
            _columns("9f3e,120x57,0,0[120x39,0,0,1,120x17,0,40,2]")
        )
        self.assertTrue(plan.balanced)
        self.assertEqual((), plan.commands)

    def test_empty_is_balanced(self) -> None:
        plan = build_cockpit_rebalance_plan([])
        self.assertTrue(plan.balanced)
        self.assertEqual(0, plan.column_count)

    def test_window_name_is_cockpit(self) -> None:
        plan = build_cockpit_rebalance_plan(_columns(_clean_skew_layout()))
        self.assertEqual(COCKPIT_WINDOW, plan.window)

    def test_as_dict_is_json_round_trippable(self) -> None:
        plan = self._plan(_clean_skew_layout())
        payload = json.loads(json.dumps(plan.as_dict()))
        self.assertFalse(payload["balanced"])
        self.assertFalse(payload["drift"])
        self.assertEqual(5, payload["column_count"])
        self.assertEqual(11, payload["columns"][0]["delta"])  # grow 44 -> 55
        self.assertEqual(4, len(payload["commands"]))


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRun:
    """A run_tmux-style callable recording argv, optionally failing on a predicate."""

    def __init__(self, fail_when=None):
        self.calls = []
        self.fail_when = fail_when or (lambda argv: False)

    def __call__(self, *argv, check=False):
        self.calls.append(tuple(argv))
        rc = 1 if self.fail_when(argv) else 0
        return FakeResult(returncode=rc, stderr="boom" if rc else "")


class ExecuteRebalancePlanTest(unittest.TestCase):
    def _execute(self, run):
        from mozyo_bridge.application.commands import execute_cockpit_rebalance_plan

        plan = build_cockpit_rebalance_plan(
            top_level_columns(parse_window_layout(_clean_skew_layout()))
        )
        return execute_cockpit_rebalance_plan(plan, run)

    def test_runs_each_resize_in_order(self) -> None:
        run = RecordingRun()
        self._execute(run)
        self.assertEqual(
            [("resize-pane", "-t", "%1104", "-x", "55"),
             ("resize-pane", "-t", "%953", "-x", "55"),
             ("resize-pane", "-t", "%1093", "-x", "55"),
             ("resize-pane", "-t", "%1109", "-x", "54")],
            run.calls,
        )

    def test_fails_fast_on_nonzero(self) -> None:
        run = RecordingRun(fail_when=lambda argv: argv[0] == "resize-pane")
        with self.assertRaises(SystemExit):
            self._execute(run)


class CockpitRebalanceCommandTest(unittest.TestCase):
    """`mozyo cockpit rebalance` — preview vs confirm-gated width restore (#12135)."""

    def _args(self, **over):
        base = dict(
            action="rebalance", cockpit_session=None,
            dry_run=False, json_output=False, confirm=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _run_cmd(self, args, *, layout, run=None):
        from mozyo_bridge.application import commands

        run = run or RecordingRun()
        buf = io.StringIO()
        with patch.object(
            commands, "_read_cockpit_window_layout", return_value=layout
        ), patch.object(commands, "require_tmux") as require_tmux, \
                patch.object(commands, "run_tmux", side_effect=run):
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_cockpit(args)
        return rc, buf.getvalue(), run, require_tmux

    def test_preview_lists_resize_plan_without_mutating(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(), layout=_clean_skew_layout()
        )
        self.assertEqual(0, rc)
        self.assertIn("preview", out)
        self.assertIn("resize-pane", out)
        self.assertIn("--confirm", out)
        require_tmux.assert_not_called()
        self.assertEqual([], run.calls)

    def test_json_preview_emits_plan_and_would_execute(self) -> None:
        rc, out, run, _ = self._run_cmd(
            self._args(json_output=True, confirm=True), layout=_clean_skew_layout()
        )
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        self.assertFalse(payload["balanced"])
        self.assertFalse(payload["drift"])
        self.assertEqual(4, len(payload["plan"]["commands"]))
        self.assertEqual([], run.calls)

    def test_confirm_applies_resize_plan(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(confirm=True), layout=_clean_skew_layout()
        )
        self.assertEqual(0, rc)
        require_tmux.assert_called_once()
        resize_calls = [c for c in run.calls if c and c[0] == "resize-pane"]
        self.assertEqual(4, len(resize_calls))
        self.assertFalse(any(c[0] == "set-option" for c in run.calls))
        self.assertFalse(any(c[0] == "select-layout" for c in run.calls))

    def test_drift_preview_reports_blocked(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(), layout=_degenerate_layout()
        )
        self.assertEqual(0, rc)
        self.assertIn("cannot rebalance", out)
        self.assertIn("reconcile", out)
        require_tmux.assert_not_called()
        self.assertEqual([], run.calls)

    def test_drift_confirm_fails_closed_without_resize(self) -> None:
        from mozyo_bridge.application import commands

        run = RecordingRun()
        with patch.object(
            commands, "_read_cockpit_window_layout",
            return_value=_degenerate_layout(),
        ), patch.object(commands, "require_tmux"), \
                patch.object(commands, "run_tmux", side_effect=run):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    commands.cmd_cockpit(self._args(confirm=True))
        self.assertFalse(any(c and c[0] == "resize-pane" for c in run.calls))

    def test_confirm_on_balanced_cockpit_is_noop(self) -> None:
        layout = (
            "abcd,81x57,0,0{"
            + _clean_column(width=40, x=0, codex=10, claude=11)
            + ","
            + _clean_column(width=40, x=41, codex=20, claude=21)
            + "}"
        )
        rc, out, run, require_tmux = self._run_cmd(
            self._args(confirm=True), layout=layout
        )
        self.assertEqual(0, rc)
        self.assertIn("already balanced", out)
        require_tmux.assert_not_called()
        self.assertEqual([], run.calls)

    def test_absent_cockpit_is_benign(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(self._args(), layout=None)
        self.assertEqual(0, rc)
        self.assertIn("nothing to rebalance", out)
        require_tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
