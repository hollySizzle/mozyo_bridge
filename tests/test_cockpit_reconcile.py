"""Cockpit structural reconcile — flatten nested cells to per-Unit columns (Redmine #12136).

`mozyo cockpit reconcile` repairs a layout-tree drift where two Units are nested
inside one tmux top-level cell (a 2x2 grid) into clean per-Unit columns, so #12135
rebalance can run. It is order-preserving: `swap-pane` sorts the live pane order,
then one checksum-valid `select-layout` lays each Unit out as a `[codex/claude]`
column in its existing left-to-right order. These tests pin the checksum, the
layout builder, the swap planner, the reconcile planner (drift / clean / the
fail-closed guards: unidentified pane, duplicate same-role, split Unit), the
fail-fast executor, and the preview/confirm CLI wiring — all hermetic. A real-tmux
scratch smoke (recorded in the issue) confirmed the end-to-end apply. Synthetic,
neutral identifiers only.
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
    build_cockpit_reconcile_plan,
    build_unit_columns_layout,
    format_custom_layout,
    layout_checksum,
    parse_window_layout,
    plan_pane_swaps,
    ReconcileUnit,
)


# A nested 2x2 cell A (Units wsA + wsB) followed by a clean cell D (wsB-less wsD).
_DEGENERATE = (
    "a3cd,200x50,0,0{"
    "100x50,0,0[100x29,0,0{50x29,0,0,1104,49x29,51,0,953},"
    "100x20,0,30{50x20,0,30,1106,49x20,51,30,954}],"
    "99x50,101,0[99x29,101,0,1093,99x20,101,30,1094]}"
)
# A clean cockpit: every top-level cell is one Unit.
_CLEAN = (
    "abcd,200x50,0,0{"
    "100x50,0,0[100x29,0,0,1104,100x20,0,30,1106],"
    "99x50,101,0[99x29,101,0,1093,99x20,101,30,1094]}"
)


def _identity(mapping):
    """Build {pane_id: {workspace_id, lane_id, role}} from a compact spec."""
    return {
        pid: {"workspace_id": ws, "lane_id": "default", "role": role}
        for pid, (ws, role) in mapping.items()
    }


_DEGEN_ID = _identity({
    "%1104": ("wsA", "codex"), "%1106": ("wsA", "claude"),
    "%953": ("wsB", "codex"), "%954": ("wsB", "claude"),
    "%1093": ("wsD", "codex"), "%1094": ("wsD", "claude"),
})


class ChecksumTest(unittest.TestCase):
    def test_known_checksum(self) -> None:
        # Validated against live tmux: this body dumps with checksum 490e.
        body = "200x50,0,0{100x50,0,0[100x29,0,0,1155,100x20,0,30,1156],99x50,101,0,1157}"
        self.assertEqual("490e", f"{layout_checksum(body):04x}")

    def test_format_prefixes_checksum(self) -> None:
        body = "80x24,0,0,1"
        self.assertEqual(f"{layout_checksum(body):04x},{body}", format_custom_layout(body))


class PlanSwapsTest(unittest.TestCase):
    def test_no_swaps_when_already_ordered(self) -> None:
        self.assertEqual([], plan_pane_swaps(["%1", "%2", "%3"], ["%1", "%2", "%3"]))

    def test_single_swap(self) -> None:
        # current A,B,A2,B2 -> desired A,A2,B,B2 needs one swap (B<->A2).
        swaps = plan_pane_swaps(["%a", "%b", "%a2", "%b2"], ["%a", "%a2", "%b", "%b2"])
        self.assertEqual([("%b", "%a2")], swaps)

    def test_swaps_realize_desired_order(self) -> None:
        cur = ["%1", "%2", "%3", "%4", "%5"]
        desired = ["%5", "%3", "%1", "%4", "%2"]
        order = list(cur)
        for a, b in plan_pane_swaps(cur, desired):
            ia, ib = order.index(a), order.index(b)
            order[ia], order[ib] = order[ib], order[ia]
        self.assertEqual(desired, order)


class BuildUnitColumnsLayoutTest(unittest.TestCase):
    def _units(self):
        return [
            ReconcileUnit("wsA", "default", "%1104", "%1106", ("%1104", "%1106"), 0, 0),
            ReconcileUnit("wsB", "default", "%953", "%954", ("%953", "%954"), 51, 0),
            ReconcileUnit("wsD", "default", "%1093", "%1094", ("%1093", "%1094"), 101, 1),
        ]

    def test_leaf_order_is_column_major_codex_first(self) -> None:
        _, order = build_unit_columns_layout(
            self._units(), window_width=200, window_height=50
        )
        self.assertEqual(["%1104", "%1106", "%953", "%954", "%1093", "%1094"], order)

    def test_body_is_balanced_braces_and_checksums(self) -> None:
        body, _ = build_unit_columns_layout(
            self._units(), window_width=200, window_height=50
        )
        self.assertEqual(body.count("{"), body.count("}"))  # the brace-count bug guard
        # tmux accepts the layout only if the checksum is valid; just confirm it
        # composes (real-tmux acceptance is covered by the scratch smoke).
        self.assertTrue(format_custom_layout(body).split(",", 1)[0])

    def test_widths_sum_with_borders_to_window(self) -> None:
        body, _ = build_unit_columns_layout(
            self._units(), window_width=200, window_height=50
        )
        # Top-level column widths + (n-1) borders == window width.
        import re
        top = body.split("{", 1)[1]
        widths = [int(m) for m in re.findall(r"(\d+)x50,\d+,0[\[,]", top)]
        self.assertEqual(3, len(widths))
        self.assertEqual(200, sum(widths) + (len(widths) - 1))

    def test_single_pane_unit_is_a_leaf_column(self) -> None:
        units = [
            ReconcileUnit("wsA", "default", "%1", "", ("%1",), 0, 0),
            ReconcileUnit("wsB", "default", "%2", "%3", ("%2", "%3"), 51, 0),
        ]
        body, order = build_unit_columns_layout(units, window_width=120, window_height=40)
        self.assertEqual(["%1", "%2", "%3"], order)

    def test_ratio_propagates_to_codex_height(self) -> None:
        # codex_ratio drives the codex pane height of each rebuilt column.
        tall, _ = build_unit_columns_layout(
            self._units(), window_width=200, window_height=101, codex_ratio=80
        )
        short, _ = build_unit_columns_layout(
            self._units(), window_width=200, window_height=101, codex_ratio=20
        )
        self.assertNotEqual(tall, short)


class BuildReconcilePlanTest(unittest.TestCase):
    def _plan(self, layout, identity, **kw):
        return build_cockpit_reconcile_plan(
            parse_window_layout(layout), identity, session="mozyo-cockpit", **kw
        )

    def test_tangled_cell_is_drift(self) -> None:
        plan = self._plan(_DEGENERATE, _DEGEN_ID)
        self.assertTrue(plan.drift)
        self.assertFalse(plan.clean)
        self.assertIsNone(plan.blocked_reason)
        # Three Units, original order preserved.
        self.assertEqual(
            [("wsA", "default"), ("wsB", "default"), ("wsD", "default")],
            [tuple(u) for u in plan.units_in_order],
        )

    def test_plan_has_swaps_then_select_layout(self) -> None:
        plan = self._plan(_DEGENERATE, _DEGEN_ID)
        self.assertTrue(plan.swap_commands)
        self.assertIsNotNone(plan.layout_command)
        self.assertEqual("select-layout", plan.layout_command.argv[0])
        # The whole plan is swap-pane(s) then exactly one select-layout; no kill.
        verbs = [c.argv[0] for c in plan.commands]
        self.assertEqual("select-layout", verbs[-1])
        self.assertTrue(all(v == "swap-pane" for v in verbs[:-1]))
        self.assertNotIn("kill-pane", verbs)

    def test_target_layout_has_valid_checksum_prefix(self) -> None:
        plan = self._plan(_DEGENERATE, _DEGEN_ID)
        csum, _, body = plan.target_layout.partition(",")
        self.assertEqual(csum, f"{layout_checksum(body):04x}")

    def test_clean_cockpit_is_no_op(self) -> None:
        plan = self._plan(_CLEAN, _DEGEN_ID)
        self.assertFalse(plan.drift)
        self.assertTrue(plan.clean)
        self.assertEqual((), plan.commands)

    def test_unidentified_pane_blocks(self) -> None:
        identity = dict(_DEGEN_ID)
        identity.pop("%954")  # %954 now has no identity
        plan = self._plan(_DEGENERATE, identity)
        self.assertTrue(plan.blocked_reason)
        self.assertIn("adopt", plan.blocked_reason)
        self.assertEqual((), plan.commands)

    def test_duplicate_same_role_blocks(self) -> None:
        # %954 mislabeled codex -> wsB has two codex panes.
        identity = dict(_DEGEN_ID)
        identity["%954"] = {"workspace_id": "wsB", "lane_id": "default", "role": "codex"}
        plan = self._plan(_DEGENERATE, identity)
        self.assertTrue(plan.blocked_reason)
        self.assertIn("same", plan.blocked_reason)
        self.assertEqual((), plan.commands)

    def test_absent_layout_is_clean_no_op(self) -> None:
        plan = build_cockpit_reconcile_plan(None, {}, session="mozyo-cockpit")
        self.assertTrue(plan.clean)
        self.assertEqual((), plan.commands)

    def test_as_dict_is_json_round_trippable(self) -> None:
        plan = self._plan(_DEGENERATE, _DEGEN_ID)
        payload = json.loads(json.dumps(plan.as_dict()))
        self.assertTrue(payload["drift"])
        self.assertEqual(2, payload["cell_count"])
        self.assertIsNotNone(payload["target_layout"])


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRun:
    def __init__(self, fail_when=None):
        self.calls = []
        self.fail_when = fail_when or (lambda argv: False)

    def __call__(self, *argv, check=False):
        self.calls.append(tuple(argv))
        rc = 1 if self.fail_when(argv) else 0
        return FakeResult(returncode=rc, stderr="boom" if rc else "")


class ExecuteReconcilePlanTest(unittest.TestCase):
    def _plan(self):
        return build_cockpit_reconcile_plan(
            parse_window_layout(_DEGENERATE), _DEGEN_ID, session="mozyo-cockpit"
        )

    def test_runs_swaps_then_select_layout_no_kill(self) -> None:
        from mozyo_bridge.application.commands import execute_cockpit_reconcile_plan

        run = RecordingRun()
        execute_cockpit_reconcile_plan(self._plan(), run)
        verbs = [c[0] for c in run.calls]
        self.assertEqual("select-layout", verbs[-1])
        self.assertNotIn("kill-pane", verbs)

    def test_fails_fast_on_nonzero(self) -> None:
        from mozyo_bridge.application.commands import execute_cockpit_reconcile_plan

        run = RecordingRun(fail_when=lambda argv: argv[0] == "select-layout")
        with self.assertRaises(SystemExit):
            execute_cockpit_reconcile_plan(self._plan(), run)

    def test_select_layout_failure_after_swaps_is_recoverable(self) -> None:
        # Residual risk (#12136 j#59862/j#59867): if swaps land but select-layout
        # fails, panes are only reordered (no kill). The executor must (a) have
        # already attempted the swaps and (b) abort with a recovery message that
        # says no pane was killed and to re-run — so the state is recoverable.
        from mozyo_bridge.application.commands import execute_cockpit_reconcile_plan

        plan = self._plan()
        self.assertTrue(plan.swap_commands)  # this fixture does require a swap
        run = RecordingRun(fail_when=lambda argv: argv[0] == "select-layout")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                execute_cockpit_reconcile_plan(plan, run)
        self.assertTrue(any(c[0] == "swap-pane" for c in run.calls))  # swaps attempted
        self.assertFalse(any(c[0] == "kill-pane" for c in run.calls))  # nothing killed
        self.assertIn("No pane was killed", err.getvalue())
        self.assertIn("re-run", err.getvalue())


class CockpitReconcileCommandTest(unittest.TestCase):
    """`mozyo cockpit reconcile` — preview vs confirm-gated structural repair (#12136)."""

    def _args(self, **over):
        base = dict(
            action="reconcile", cockpit_session=None, codex_ratio=70,
            dry_run=False, json_output=False, confirm=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _run_cmd(self, args, *, layout, identity, run=None):
        from mozyo_bridge.application import commands

        run = run or RecordingRun()
        geom = [
            {"pane_id": pid, "workspace_id": v["workspace_id"],
             "role": v["role"], "lane_id": v["lane_id"]}
            for pid, v in identity.items()
        ] if identity is not None else None
        buf = io.StringIO()
        with patch.object(
            commands, "_read_cockpit_window_layout", return_value=layout
        ), patch.object(commands, "_read_cockpit_geometry", return_value=geom), \
                patch.object(commands, "require_tmux") as require_tmux, \
                patch.object(commands, "run_tmux", side_effect=run):
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_cockpit(args)
        return rc, buf.getvalue(), run, require_tmux

    def test_preview_reports_plan_without_mutating(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(), layout=_DEGENERATE, identity=_DEGEN_ID
        )
        self.assertEqual(0, rc)
        self.assertIn("preview", out)
        self.assertIn("tangled", out)
        self.assertIn("select-layout", out)
        self.assertIn("--confirm", out)
        require_tmux.assert_not_called()
        self.assertEqual([], run.calls)

    def test_json_preview_reports_would_execute(self) -> None:
        rc, out, run, _ = self._run_cmd(
            self._args(json_output=True, confirm=True), layout=_DEGENERATE,
            identity=_DEGEN_ID,
        )
        payload = json.loads(out)
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        self.assertTrue(payload["drift"])
        # Normalized audit fields (#12136 j#59871): blocked_reason naming +
        # current/target layout for before/after auditing.
        self.assertIn("blocked_reason", payload)
        self.assertIsNone(payload["blocked_reason"])
        self.assertEqual(_DEGENERATE, payload["current_layout"])
        self.assertTrue(payload["current_cells"])
        self.assertEqual(
            payload["target_layout"].split(",", 1)[0],
            payload["target_layout_checksum"],
        )
        self.assertEqual([], run.calls)

    def test_json_blocked_uses_blocked_reason_field(self) -> None:
        identity = dict(_DEGEN_ID)
        identity.pop("%954")
        rc, out, run, _ = self._run_cmd(
            self._args(json_output=True), layout=_DEGENERATE, identity=identity
        )
        payload = json.loads(out)
        self.assertIn("blocked_reason", payload)
        self.assertIn("adopt", payload["blocked_reason"])
        self.assertNotIn("blocked", {k for k in payload if k != "blocked_reason"})

    def test_confirm_applies_swaps_and_select_layout(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(confirm=True), layout=_DEGENERATE, identity=_DEGEN_ID
        )
        self.assertEqual(0, rc)
        require_tmux.assert_called_once()
        verbs = [c[0] for c in run.calls if c]
        self.assertIn("select-layout", verbs)
        self.assertFalse(any(v == "kill-pane" for v in verbs))

    def test_unidentified_confirm_fails_closed(self) -> None:
        from mozyo_bridge.application import commands

        identity = dict(_DEGEN_ID)
        identity.pop("%954")
        run = RecordingRun()
        geom = [
            {"pane_id": pid, "workspace_id": v["workspace_id"],
             "role": v["role"], "lane_id": v["lane_id"]}
            for pid, v in identity.items()
        ]
        with patch.object(
            commands, "_read_cockpit_window_layout", return_value=_DEGENERATE
        ), patch.object(commands, "_read_cockpit_geometry", return_value=geom), \
                patch.object(commands, "require_tmux"), \
                patch.object(commands, "run_tmux", side_effect=run):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    commands.cmd_cockpit(self._args(confirm=True))
        self.assertFalse(any(c and c[0] == "select-layout" for c in run.calls))

    def test_clean_cockpit_confirm_is_noop(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(confirm=True), layout=_CLEAN, identity=_DEGEN_ID
        )
        self.assertEqual(0, rc)
        self.assertIn("nothing to do", out)
        require_tmux.assert_not_called()
        self.assertEqual([], run.calls)

    def test_unparseable_present_layout_fails_closed(self) -> None:
        # A non-empty but unparseable layout must not be reported as clean.
        rc, out, run, require_tmux = self._run_cmd(
            self._args(), layout="garbage-not-a-layout", identity=_DEGEN_ID
        )
        self.assertEqual(0, rc)
        self.assertIn("could not parse", out)
        self.assertEqual([], run.calls)

    def test_unparseable_present_layout_json_matches_audit_contract(self) -> None:
        # Regression (#12136 j#59881): the malformed-layout --json branch must
        # carry the SAME audit fields as the normal branch (blocked_reason +
        # current_layout/current_cells/target_layout/target_layout_checksum), and
        # must not expose a top-level `blocked`.
        rc, out, run, _ = self._run_cmd(
            self._args(json_output=True), layout="garbage-not-a-layout",
            identity=_DEGEN_ID,
        )
        self.assertEqual(0, rc)
        payload = json.loads(out)
        for field in (
            "blocked_reason", "current_layout", "current_cells",
            "target_layout", "target_layout_checksum",
        ):
            self.assertIn(field, payload)
        self.assertNotIn("blocked", payload)
        self.assertEqual("garbage-not-a-layout", payload["current_layout"])
        self.assertEqual([], payload["current_cells"])
        self.assertIsNone(payload["target_layout"])
        self.assertIsNone(payload["target_layout_checksum"])
        self.assertIn("could not parse", payload["blocked_reason"])
        self.assertEqual([], run.calls)

    def test_absent_cockpit_is_benign(self) -> None:
        rc, out, run, require_tmux = self._run_cmd(
            self._args(), layout=None, identity=None
        )
        self.assertEqual(0, rc)
        self.assertIn("nothing to reconcile", out)
        require_tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
