"""Cockpit reconcile boundary — fake-port use case tests (Redmine #13008).

Pins the #13008 carve of the pane-identity projection / preview rendering /
confirm-gated handler out of ``commands.py`` into
:mod:`mozyo_bridge.application.cockpit_reconcile_command`. Everything runs
against a fake :class:`CockpitReconcileOps` port (no tmux, no monkeypatch); the
``commands.*`` thin-wrapper seams stay pinned by the existing
``test_cockpit_reconcile`` characterization suite. Synthetic, neutral
identifiers only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_reconcile_command import (
    CockpitReconcileOps,
    CockpitReconcileUseCase,
    LiveCockpitReconcileOps,
    project_pane_identity,
    render_reconcile_preview_lines,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    build_cockpit_reconcile_plan,
    parse_window_layout,
)


# A nested 2x2 cell A (Units wsA + wsB) followed by a clean cell D (wsD).
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


def _geometry(identity):
    """The pane-geometry read shape ``project_pane_identity`` consumes."""
    return [
        {"pane_id": pid, "workspace_id": v["workspace_id"],
         "role": v["role"], "lane_id": v["lane_id"]}
        for pid, v in identity.items()
    ]


def _plan(layout=_DEGENERATE, identity=_DEGEN_ID, session="mozyo-cockpit"):
    return build_cockpit_reconcile_plan(
        parse_window_layout(layout), identity, session=session, codex_ratio=70
    )


class FakeReconcileOps:
    """Recording :class:`CockpitReconcileOps` fake — no tmux."""

    def __init__(self, *, layout=_DEGENERATE, identity=_DEGEN_ID,
                 after_columns=("c1", "c2", "c3")):
        self.layout = layout
        self.geometry = _geometry(identity) if identity is not None else None
        self.after_columns = tuple(after_columns)
        self.emitted: list[str] = []
        self.require_tmux_calls = 0
        self.executed: list = []
        self.died: list[str] = []

    def read_window_layout(self, session):
        return self.layout

    def read_geometry(self, session):
        return self.geometry

    def require_tmux(self):
        self.require_tmux_calls += 1

    def execute_reconcile(self, plan):
        self.executed.append(plan)

    def rebalance_columns(self, session):
        return True, self.after_columns

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

    def emit(self, text):
        self.emitted.append(text)


class PortContractTest(unittest.TestCase):
    def test_live_and_fake_satisfy_port(self) -> None:
        self.assertIsInstance(LiveCockpitReconcileOps(), CockpitReconcileOps)
        self.assertIsInstance(FakeReconcileOps(), CockpitReconcileOps)


class ProjectPaneIdentityTest(unittest.TestCase):
    def test_projects_identity_fields(self) -> None:
        panes = [
            {"pane_id": "%1", "workspace_id": "wsA", "role": "codex",
             "lane_id": "default", "pane_left": 0},
        ]
        self.assertEqual(
            {"%1": {"workspace_id": "wsA", "lane_id": "default", "role": "codex"}},
            project_pane_identity(panes),
        )

    def test_skips_panes_without_pane_id_and_defaults_missing_fields(self) -> None:
        panes = [
            {"pane_id": "", "workspace_id": "wsA"},
            {"workspace_id": "wsB"},
            {"pane_id": "%2"},
        ]
        self.assertEqual(
            {"%2": {"workspace_id": "", "lane_id": "", "role": ""}},
            project_pane_identity(panes),
        )

    def test_absent_pane_list_yields_empty_map(self) -> None:
        self.assertEqual({}, project_pane_identity(None))
        self.assertEqual({}, project_pane_identity([]))


class PureRenderingTest(unittest.TestCase):
    def test_drifted_plan_renders_cells_commands_and_confirm_hint(self) -> None:
        lines = render_reconcile_preview_lines(_plan(), "mozyo-cockpit")
        text = "\n".join(lines)
        self.assertIn(
            "cockpit reconcile (preview; no tmux changes): session=mozyo-cockpit",
            lines[0],
        )
        self.assertIn("[tangled: >1 Unit in one cell]", text)
        self.assertIn("target Unit columns (left-to-right, order preserved):", text)
        self.assertIn("select-layout", text)
        self.assertIn("  run `mozyo cockpit reconcile --confirm` to apply.", lines[-1])

    def test_blocked_plan_renders_cannot_reconcile(self) -> None:
        identity = dict(_DEGEN_ID)
        identity.pop("%954")
        lines = render_reconcile_preview_lines(
            _plan(identity=identity), "mozyo-cockpit"
        )
        text = "\n".join(lines)
        self.assertIn("  cannot reconcile:", text)
        self.assertIn("unidentified=", text)
        self.assertNotIn("--confirm` to apply", text)

    def test_clean_plan_renders_nothing_to_reconcile(self) -> None:
        lines = render_reconcile_preview_lines(_plan(layout=_CLEAN), "mozyo-cockpit")
        self.assertIn(
            "  already one Unit per top-level column — nothing to reconcile.", lines
        )


class HandleReconcileUseCaseTest(unittest.TestCase):
    def _handle(self, ops, *, confirm=False, json_output=False, dry_run=False):
        rc = CockpitReconcileUseCase(ops).handle(
            "mozyo-cockpit", confirm=confirm, json_output=json_output,
            dry_run=dry_run, codex_ratio=70,
        )
        return rc, ops

    def test_json_is_single_parseable_preview_document(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(), confirm=True, json_output=True)
        self.assertEqual(0, rc)
        payload = json.loads(ops.emitted[0])
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        self.assertTrue(payload["drift"])
        self.assertIsNone(payload["blocked_reason"])
        self.assertEqual(_DEGENERATE, payload["current_layout"])
        self.assertEqual(
            payload["target_layout"].split(",", 1)[0],
            payload["target_layout_checksum"],
        )
        self.assertEqual([], ops.executed)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_json_blocked_reports_blocked_reason(self) -> None:
        identity = dict(_DEGEN_ID)
        identity.pop("%954")
        rc, ops = self._handle(
            FakeReconcileOps(identity=identity), json_output=True
        )
        self.assertEqual(0, rc)
        payload = json.loads(ops.emitted[0])
        self.assertIn("adopt", payload["blocked_reason"])
        self.assertFalse(payload["would_execute"])
        self.assertEqual([], ops.executed)

    def test_bare_preview_reports_plan_without_mutating(self) -> None:
        rc, ops = self._handle(FakeReconcileOps())
        self.assertEqual(0, rc)
        text = "\n".join(ops.emitted)
        self.assertIn("preview", text)
        self.assertIn("tangled", text)
        self.assertIn("--confirm` to apply", text)
        self.assertEqual([], ops.executed)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_dry_run_outranks_confirm(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(), confirm=True, dry_run=True)
        self.assertEqual(0, rc)
        self.assertIn("preview; no tmux changes", "\n".join(ops.emitted))
        self.assertEqual([], ops.executed)

    def test_confirm_executes_plan_and_reports_columns(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(), confirm=True)
        self.assertEqual(0, rc)
        self.assertEqual(1, ops.require_tmux_calls)
        self.assertEqual(1, len(ops.executed))
        text = "\n".join(ops.emitted)
        self.assertIn("flattening nested cells into 3 per-Unit columns", text)
        self.assertIn("  reconciled: 3 top-level columns now align with Units;", text)

    def test_confirm_blocked_dies_before_executing(self) -> None:
        identity = dict(_DEGEN_ID)
        identity.pop("%954")
        ops = FakeReconcileOps(identity=identity)
        with self.assertRaises(SystemExit):
            self._handle(ops, confirm=True)
        self.assertEqual(1, len(ops.died))
        self.assertEqual([], ops.executed)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_clean_cockpit_confirm_is_benign_noop(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(layout=_CLEAN), confirm=True)
        self.assertEqual(0, rc)
        self.assertIn("nothing to do", "\n".join(ops.emitted))
        self.assertEqual([], ops.executed)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_unparseable_present_layout_fails_closed_in_preview(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(layout="garbage-not-a-layout"))
        self.assertEqual(0, rc)
        self.assertIn("could not parse", "\n".join(ops.emitted))
        self.assertEqual([], ops.executed)
        self.assertEqual([], ops.died)

    def test_unparseable_present_layout_confirm_dies(self) -> None:
        ops = FakeReconcileOps(layout="garbage-not-a-layout")
        with self.assertRaises(SystemExit):
            self._handle(ops, confirm=True)
        self.assertEqual(1, len(ops.died))
        self.assertIn("could not parse", ops.died[0])
        self.assertEqual([], ops.executed)

    def test_unparseable_present_layout_json_matches_audit_contract(self) -> None:
        rc, ops = self._handle(
            FakeReconcileOps(layout="garbage-not-a-layout"), json_output=True
        )
        self.assertEqual(0, rc)
        payload = json.loads(ops.emitted[0])
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
        self.assertEqual([], ops.executed)

    def test_absent_cockpit_is_benign(self) -> None:
        rc, ops = self._handle(FakeReconcileOps(layout=None, identity=None))
        self.assertEqual(0, rc)
        self.assertIn("nothing to reconcile", "\n".join(ops.emitted))
        self.assertEqual([], ops.executed)


if __name__ == "__main__":
    unittest.main()
