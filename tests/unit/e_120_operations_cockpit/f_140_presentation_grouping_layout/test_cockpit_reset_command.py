"""Cockpit reset/rebuild boundary — fake-port use case tests (Redmine #12989).

Pins the #12989 carve of the reset grade read / inventory rendering /
confirm-gated handler out of ``commands.py`` into
:mod:`mozyo_bridge.application.cockpit_reset_command`. Everything runs against
a fake :class:`CockpitResetOps` port (no tmux, no monkeypatch); the
``commands.*`` thin-wrapper seams (including the ``os.execvp`` attach tail)
stay pinned by the existing ``test_cockpit_reset`` characterization suite.
Synthetic, neutral identifiers only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_reset_command import (
    CockpitResetOps,
    CockpitResetUseCase,
    LiveCockpitResetOps,
    cockpit_extra_windows,
    render_reset_inventory_lines,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    COCKPIT_WINDOW,
    CockpitWorkspace,
)


def _ws(**over):
    base = dict(
        workspace_id="wsX", label="mozyo-ws", repo_root="/workspace/project-alpha",
        lane_id="default", lane_label=None,
    )
    base.update(over)
    return CockpitWorkspace(**base)


def _column(pane_id="%1", *, workspace_id="wsX", role="codex", lane_id="default"):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": 0,
        "pane_width": 100,
    }


class FakeResetOps:
    """Recording :class:`CockpitResetOps` fake — no tmux."""

    def __init__(self, *, clients=(), clients_known=True, windows=(COCKPIT_WINDOW,)):
        self.clients = tuple(clients)
        self.clients_known = clients_known
        self.windows = tuple(windows)
        self.emitted: list[str] = []
        self.require_tmux_calls = 0
        self.reset_plans: list = []
        self.create_plans: list = []
        self.died: list[str] = []

    def session_attached_clients_result(self, session):
        return self.clients, self.clients_known

    def list_session_windows(self, session):
        return list(self.windows)

    def require_tmux(self):
        self.require_tmux_calls += 1

    def execute_reset(self, plan):
        self.reset_plans.append(plan)

    def execute_create(self, plan):
        self.create_plans.append(plan)
        return {}

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

    def emit(self, text):
        self.emitted.append(text)


class PortContractTest(unittest.TestCase):
    def test_live_and_fake_satisfy_port(self) -> None:
        self.assertIsInstance(LiveCockpitResetOps(), CockpitResetOps)
        self.assertIsInstance(FakeResetOps(), CockpitResetOps)


class PureRenderingTest(unittest.TestCase):
    def _target(self, **over):
        base = dict(
            attached_clients=(),
            windows=(COCKPIT_WINDOW, "Alpha", "Beta"),
            managed_panes=(
                SimpleNamespace(
                    pane_id="%1", workspace_id="wsX", role="codex", lane_id="default"
                ),
            ),
            unmanaged_panes=(SimpleNamespace(pane_id="%7", role=None),),
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_extra_windows_excludes_cockpit_home(self) -> None:
        self.assertEqual(["Alpha", "Beta"], cockpit_extra_windows(self._target()))
        self.assertEqual(
            [], cockpit_extra_windows(self._target(windows=(COCKPIT_WINDOW, "")))
        )

    def test_inventory_lines_render_clients_windows_and_panes(self) -> None:
        lines = render_reset_inventory_lines(self._target())
        text = "\n".join(lines)
        self.assertIn("  attached clients: none", text)
        self.assertIn("Alpha, Beta", text)
        self.assertIn("`kill-session` also destroys 2 other window(s)", text)
        self.assertIn(
            "  pane %1: workspace=wsX role=codex lane=default (mozyo-managed)", text
        )
        self.assertIn("  pane %7: role=- (NOT mozyo-managed)", text)

    def test_inventory_warning_absent_without_extra_windows(self) -> None:
        lines = render_reset_inventory_lines(self._target(windows=(COCKPIT_WINDOW,)))
        self.assertNotIn("also destroys", "\n".join(lines))


class AssessUseCaseTest(unittest.TestCase):
    def test_absent_session_grades_without_reads(self) -> None:
        target = CockpitResetUseCase(FakeResetOps()).assess(
            "mozyo-cockpit", columns=None, session_present=False
        )
        self.assertTrue(target.absent)
        self.assertFalse(target.resettable)

    def test_managed_detached_session_is_resettable(self) -> None:
        target = CockpitResetUseCase(FakeResetOps()).assess(
            "mozyo-cockpit", columns=[_column()], session_present=True
        )
        self.assertTrue(target.resettable)

    def test_attached_client_blocks_fail_closed(self) -> None:
        target = CockpitResetUseCase(FakeResetOps(clients=("/dev/ttys003",))).assess(
            "mozyo-cockpit", columns=[_column()], session_present=True
        )
        self.assertFalse(target.resettable)


class HandleResetUseCaseTest(unittest.TestCase):
    def _handle(self, ops, *, confirm=False, json_output=False, dry_run=False,
                no_attach=False, rebuild=False, columns=..., session_present=True):
        args = SimpleNamespace(
            confirm=confirm, json_output=json_output, dry_run=dry_run,
            no_attach=no_attach,
        )
        outcome = CockpitResetUseCase(ops).handle(
            args, _ws(), "mozyo-cockpit",
            columns=[_column()] if columns is ... else columns,
            session_present=session_present, rebuild=rebuild, launch=None,
            codex_ratio=70,
        )
        return outcome, ops

    def test_json_is_single_parseable_preview_document(self) -> None:
        outcome, ops = self._handle(FakeResetOps(), confirm=True, json_output=True)
        self.assertEqual(0, outcome.exit_code)
        self.assertIsNone(outcome.attach_session)
        payload = json.loads(ops.emitted[0])
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        self.assertEqual([], ops.reset_plans)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_bare_preview_shows_inventory_and_plan_without_mutation(self) -> None:
        outcome, ops = self._handle(FakeResetOps())
        self.assertEqual(0, outcome.exit_code)
        text = "\n".join(ops.emitted)
        self.assertIn("cockpit reset (preview; no tmux changes)", text)
        self.assertIn("  attached clients: none", text)
        self.assertIn("run `mozyo cockpit reset --confirm` to execute.", text)
        self.assertEqual([], ops.reset_plans)

    def test_confirm_reset_kills_managed_cockpit(self) -> None:
        outcome, ops = self._handle(FakeResetOps(), confirm=True)
        self.assertEqual(0, outcome.exit_code)
        self.assertIsNone(outcome.attach_session)
        self.assertEqual(1, ops.require_tmux_calls)
        self.assertEqual(1, len(ops.reset_plans))
        self.assertEqual([], ops.create_plans)  # reset never creates
        self.assertIn("  reset: cockpit session 'mozyo-cockpit' killed.", ops.emitted)

    def test_confirm_reset_on_absent_cockpit_is_benign_noop(self) -> None:
        outcome, ops = self._handle(
            FakeResetOps(), confirm=True, columns=None, session_present=False
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertIn("nothing to do", "\n".join(ops.emitted))
        self.assertEqual([], ops.reset_plans)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_confirm_blocked_by_attached_client_dies(self) -> None:
        ops = FakeResetOps(clients=("/dev/ttys003",))
        with self.assertRaises(SystemExit):
            self._handle(ops, confirm=True)
        self.assertEqual(1, len(ops.died))
        self.assertEqual([], ops.reset_plans)

    def test_confirm_rebuild_kills_creates_and_requests_attach(self) -> None:
        outcome, ops = self._handle(FakeResetOps(), confirm=True, rebuild=True)
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual("mozyo-cockpit", outcome.attach_session)
        self.assertEqual(1, len(ops.reset_plans))
        self.assertEqual(1, len(ops.create_plans))
        text = "\n".join(ops.emitted)
        self.assertIn("rebuilding a fresh cockpit", text)
        self.assertIn("cockpit rebuilt: session=mozyo-cockpit", text)

    def test_confirm_rebuild_no_attach_prints_attach_hint(self) -> None:
        outcome, ops = self._handle(
            FakeResetOps(), confirm=True, rebuild=True, no_attach=True
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertIsNone(outcome.attach_session)
        self.assertIn("attach: tmux -CC attach -t mozyo-cockpit", ops.emitted)

    def test_rebuild_on_absent_cockpit_is_plain_create(self) -> None:
        outcome, ops = self._handle(
            FakeResetOps(), confirm=True, rebuild=True, no_attach=True,
            columns=None, session_present=False,
        )
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual([], ops.reset_plans)  # nothing to kill
        self.assertEqual(1, len(ops.create_plans))

    def test_dry_run_outranks_confirm(self) -> None:
        outcome, ops = self._handle(FakeResetOps(), confirm=True, dry_run=True)
        self.assertEqual(0, outcome.exit_code)
        self.assertIn("preview; no tmux changes", "\n".join(ops.emitted))
        self.assertEqual([], ops.reset_plans)


if __name__ == "__main__":
    unittest.main()
