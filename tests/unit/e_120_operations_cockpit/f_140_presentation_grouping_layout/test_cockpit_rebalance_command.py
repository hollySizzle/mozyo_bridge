"""Cockpit rebalance boundary — fake-port use case tests (Redmine #13009).

Pins the #13009 carve of the preview rendering / confirm-gated `mozyo cockpit
rebalance` handler out of ``commands.py`` into
:mod:`mozyo_bridge.application.cockpit_rebalance_command`. Everything runs
against a fake :class:`CockpitRebalanceOps` port (no tmux, no monkeypatch); the
``commands.*`` thin-wrapper seams (the ``_read_cockpit_window_layout`` /
``require_tmux`` / ``run_tmux`` patches through ``cmd_cockpit``) stay pinned by
the existing ``test_cockpit_rebalance`` characterization suite. Synthetic,
neutral identifiers only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_rebalance_command import (
    CockpitRebalanceOps,
    CockpitRebalanceUseCase,
    LiveCockpitRebalanceOps,
    render_rebalance_preview_lines,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    build_cockpit_rebalance_plan,
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
    """4 clean columns of widths 44 / 40 / 78 / 55 (an uneven live skew)."""
    widths = [44, 40, 78, 55]
    panes = [(1104, 1106), (953, 954), (1093, 1094), (1111, 1112)]
    cells = []
    x = 0
    for w, (codex, claude) in zip(widths, panes):
        cells.append(_clean_column(width=w, x=x, codex=codex, claude=claude))
        x += w + 1
    return "abcd,221x57,0,0{" + ",".join(cells) + "}"


def _degenerate_layout():
    """A nested 2x2 drift cell next to a clean column (#12136 reconcile scope)."""
    cell_a = (
        "85x57,0,0[85x39,0,0{29x39,0,0,1104,55x39,30,0,953},"
        "85x17,0,40{44x17,0,40,1106,40x17,45,40,954}]"
    )
    cell_b = _clean_column(width=55, x=86, codex=1093, claude=1094)
    return f"a3cd,141x57,0,0{{{cell_a},{cell_b}}}"


def _balanced_layout():
    """Two equal-width clean columns — already within tolerance."""
    return (
        "abcd,81x57,0,0{"
        + _clean_column(width=40, x=0, codex=10, claude=11)
        + ","
        + _clean_column(width=40, x=41, codex=20, claude=21)
        + "}"
    )


def _columns(layout):
    return top_level_columns(parse_window_layout(layout))


def _plan(layout, session="mozyo-cockpit"):
    return build_cockpit_rebalance_plan(_columns(layout), session=session)


class FakeRebalanceOps:
    """Recording :class:`CockpitRebalanceOps` fake — no tmux."""

    def __init__(self, *, present=True, columns=(), after=None):
        self.present = present
        self.columns = tuple(columns)
        self.after = tuple(after) if after is not None else self.columns
        self.reads = 0
        self.emitted: list[str] = []
        self.require_tmux_calls = 0
        self.executed: list = []
        self.died: list[str] = []

    def rebalance_columns(self, session):
        self.reads += 1
        return self.present, (self.columns if self.reads == 1 else self.after)

    def require_tmux(self):
        self.require_tmux_calls += 1

    def execute_rebalance(self, plan):
        self.executed.append(plan)

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

    def emit(self, text):
        self.emitted.append(text)


class PortContractTest(unittest.TestCase):
    def test_live_and_fake_satisfy_port(self) -> None:
        self.assertIsInstance(LiveCockpitRebalanceOps(), CockpitRebalanceOps)
        self.assertIsInstance(FakeRebalanceOps(), CockpitRebalanceOps)


class RenderRebalancePreviewLinesTest(unittest.TestCase):
    """Pure preview renderer over an already-built plan."""

    def test_skew_renders_header_columns_and_resize_plan(self) -> None:
        lines = render_rebalance_preview_lines(_plan(_clean_skew_layout()))
        self.assertIn("preview; no tmux changes", lines[0])
        self.assertIn("session=mozyo-cockpit", lines[0])
        column_lines = [l for l in lines if l.startswith("  column ")]
        self.assertEqual(4, len(column_lines))
        self.assertIn("current=44", column_lines[0])
        resize_lines = [l for l in lines if "resize-pane" in l]
        # N columns need only N-1 resize commands (rightmost absorbs remainder).
        self.assertEqual(3, len(resize_lines))
        self.assertTrue(all(l.startswith("    tmux ") for l in resize_lines))
        self.assertEqual(
            "  run `mozyo cockpit rebalance --confirm` to apply.", lines[-1]
        )

    def test_drift_renders_blocked_reason_and_no_plan(self) -> None:
        plan = _plan(_degenerate_layout())
        lines = render_rebalance_preview_lines(plan)
        self.assertIn(f"  cannot rebalance: {plan.blocked_reason}", lines)
        self.assertIn(" [drift: not a clean full-width split]", "\n".join(lines))
        self.assertFalse(any("resize-pane" in l for l in lines))

    def test_balanced_renders_no_op_notice(self) -> None:
        lines = render_rebalance_preview_lines(_plan(_balanced_layout()))
        self.assertIn(
            "  already balanced within tolerance — nothing to rebalance.", lines
        )
        self.assertFalse(any("resize-pane" in l for l in lines))


class CockpitRebalanceUseCaseTest(unittest.TestCase):
    """Preview vs confirm-gated width restore through the fake port (#12135)."""

    def _handle(self, ops, *, confirm=False, json_output=False, dry_run=False):
        return CockpitRebalanceUseCase(ops).handle(
            "mozyo-cockpit",
            confirm=confirm,
            json_output=json_output,
            dry_run=dry_run,
        )

    def test_bare_preview_emits_plan_without_mutating(self) -> None:
        ops = FakeRebalanceOps(columns=_columns(_clean_skew_layout()))
        rc = self._handle(ops)
        self.assertEqual(0, rc)
        out = "\n".join(ops.emitted)
        self.assertIn("preview", out)
        self.assertIn("resize-pane", out)
        self.assertIn("--confirm", out)
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_json_preview_emits_plan_and_would_execute(self) -> None:
        ops = FakeRebalanceOps(columns=_columns(_clean_skew_layout()))
        rc = self._handle(ops, confirm=True, json_output=True)
        self.assertEqual(0, rc)
        payload = json.loads("\n".join(ops.emitted))
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        self.assertFalse(payload["balanced"])
        self.assertFalse(payload["drift"])
        self.assertEqual(3, len(payload["plan"]["commands"]))
        self.assertEqual([], ops.executed)

    def test_dry_run_wins_over_confirm(self) -> None:
        ops = FakeRebalanceOps(columns=_columns(_clean_skew_layout()))
        rc = self._handle(ops, confirm=True, dry_run=True)
        self.assertEqual(0, rc)
        self.assertIn("preview", "\n".join(ops.emitted))
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_confirm_applies_resize_plan_and_reports_after_widths(self) -> None:
        columns = _columns(_clean_skew_layout())
        after = _columns(_balanced_layout())
        ops = FakeRebalanceOps(columns=columns, after=after)
        rc = self._handle(ops, confirm=True)
        self.assertEqual(0, rc)
        self.assertEqual(1, ops.require_tmux_calls)
        self.assertEqual(1, len(ops.executed))
        self.assertFalse(ops.executed[0].drift)
        self.assertEqual(2, ops.reads)
        self.assertIn(
            f"  rebalanced: column widths now {[c.width for c in after]}.",
            ops.emitted,
        )

    def test_confirm_on_drift_fails_closed_without_resize(self) -> None:
        ops = FakeRebalanceOps(columns=_columns(_degenerate_layout()))
        with self.assertRaises(SystemExit):
            self._handle(ops, confirm=True)
        self.assertEqual(1, len(ops.died))
        self.assertIn("reconcile", ops.died[0])
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_confirm_on_balanced_cockpit_is_noop(self) -> None:
        ops = FakeRebalanceOps(columns=_columns(_balanced_layout()))
        rc = self._handle(ops, confirm=True)
        self.assertEqual(0, rc)
        self.assertIn("already balanced", "\n".join(ops.emitted))
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_absent_cockpit_is_benign(self) -> None:
        ops = FakeRebalanceOps(present=False, columns=())
        rc = self._handle(ops)
        self.assertEqual(0, rc)
        self.assertIn("nothing to rebalance", "\n".join(ops.emitted))
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_absent_cockpit_json_reports_not_present(self) -> None:
        ops = FakeRebalanceOps(present=False, columns=())
        rc = self._handle(ops, json_output=True)
        self.assertEqual(0, rc)
        payload = json.loads("\n".join(ops.emitted))
        self.assertFalse(payload["cockpit_present"])
        self.assertFalse(payload["would_execute"])


if __name__ == "__main__":
    unittest.main()
