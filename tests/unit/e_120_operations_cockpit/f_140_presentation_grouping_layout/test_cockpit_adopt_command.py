"""Cockpit adopt boundary — fake-port use case tests (Redmine #12987).

Pins the #12987 carve of the adopt observation projection / advisory /
fail-closed resolver / confirm-gated handler out of ``commands.py`` into
:mod:`mozyo_bridge.application.cockpit_adopt_command`. Everything runs against
a fake :class:`CockpitAdoptOps` port (no tmux, no inventory, no monkeypatch);
the ``commands.*`` thin-wrapper seams stay pinned by the existing
characterization suites (``test_cockpit_adopt`` / ``test_cockpit_adopt_phase2``
/ ``test_cockpit_decision``). Synthetic, neutral identifiers only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_adopt_command import (
    CockpitAdoptOps,
    CockpitAdoptUseCase,
    LiveCockpitAdoptOps,
    project_normal_session_observations,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    ADOPT_STATUS_CANDIDATE,
    ADOPT_STATUS_NONE,
    CockpitWorkspace,
    NormalSessionObservation,
    detect_adopt_candidates,
)


def _ws(**over):
    base = dict(
        workspace_id="wsX", label="mozyo-ws", repo_root="/workspace/project-alpha",
        lane_id="default", lane_label=None,
    )
    base.update(over)
    return CockpitWorkspace(**base)


def _obs(session, role, *, workspace_id="wsX", lane_id="default", pane_id="%1"):
    return NormalSessionObservation(
        session=session, workspace_id=workspace_id, lane_id=lane_id,
        role=role, pane_id=pane_id,
    )


def _candidate_advisory():
    return detect_adopt_candidates(
        workspace_id="wsX",
        lane_id="default",
        observations=[
            _obs("mozyo-ws", "codex", pane_id="%2"),
            _obs("mozyo-ws", "claude", pane_id="%3"),
        ],
    )


def _none_advisory():
    return detect_adopt_candidates(
        workspace_id="wsX", lane_id="default", observations=[]
    )


def _record(**over):
    base = dict(
        agent_kind="codex",
        session="mozyo-ws",
        role_source="window_name",
        repo_root="/workspace/project-alpha",
        pane_id="%1",
        workspace=SimpleNamespace(workspace_id="wsX", canonical_session="mozyo-ws"),
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Lane:
    lane_id = "default"


class FakeAdoptOps:
    """Recording :class:`CockpitAdoptOps` fake — no tmux / inventory / registry."""

    def __init__(self, *, snapshot=None, inventory_error=None, advisory=None,
                 attached=(), anchor="%9", lane_error=None):
        self.snapshot = snapshot
        self.inventory_error = inventory_error
        self.advisory = advisory
        self.attached = tuple(attached)
        self.anchor = anchor
        self.lane_error = lane_error
        self.emitted: list[str] = []
        self.lane_calls: list[tuple] = []
        self.require_tmux_calls = 0
        self.executed: list = []
        self.died: list[str] = []

    def take_inventory(self):
        if self.inventory_error is not None:
            raise self.inventory_error
        return self.snapshot

    def resolve_workspace_lane(self, repo_root, workspace_id):
        if self.lane_error is not None:
            raise self.lane_error
        self.lane_calls.append((repo_root, workspace_id))
        return _Lane()

    def adopt_advisory(self, workspace, cockpit_session):
        return self.advisory

    def session_attached_clients(self, session):
        return self.attached

    def rightmost_codex_anchor(self, codex_columns):
        return self.anchor

    def require_tmux(self):
        self.require_tmux_calls += 1

    def execute_adopt(self, plan):
        self.executed.append(plan)
        return {"stamp_warnings": []}

    def source_session_cleanup_note(self, source_session):
        return f"cleanup:{source_session}"

    def die(self, message):
        self.died.append(message)
        raise SystemExit(2)

    def emit(self, text):
        self.emitted.append(text)


class PortContractTest(unittest.TestCase):
    def test_live_and_fake_satisfy_port(self) -> None:
        self.assertIsInstance(LiveCockpitAdoptOps(), CockpitAdoptOps)
        self.assertIsInstance(FakeAdoptOps(), CockpitAdoptOps)


class ProjectNormalSessionObservationsTest(unittest.TestCase):
    """The pure inventory-record projection (#11897 filters + fallbacks)."""

    def _project(self, records, *, cockpit_session="mozyo-cockpit"):
        calls: list[tuple] = []

        def resolve_lane(repo_root, workspace_id):
            calls.append((repo_root, workspace_id))
            return _Lane()

        obs = project_normal_session_observations(
            records, cockpit_session=cockpit_session, resolve_lane=resolve_lane
        )
        return obs, calls

    def test_keeps_window_name_normal_agent(self) -> None:
        obs, _ = self._project([_record()])
        self.assertEqual(1, len(obs))
        self.assertEqual("mozyo-ws", obs[0].session)
        self.assertEqual("wsX", obs[0].workspace_id)
        self.assertEqual("codex", obs[0].role)
        self.assertEqual("default", obs[0].lane_id)

    def test_drops_pane_in_cockpit_session(self) -> None:
        obs, _ = self._project([_record(session="mozyo-cockpit")])
        self.assertEqual([], obs)

    def test_drops_pane_option_role_source(self) -> None:
        obs, _ = self._project([_record(role_source="pane_option")])
        self.assertEqual([], obs)

    def test_drops_unknown_agent_kind(self) -> None:
        obs, _ = self._project([_record(agent_kind="unknown")])
        self.assertEqual([], obs)

    def test_unregistered_workspace_falls_back_to_canonical_session(self) -> None:
        # Redmine #11897 review j#57857: workspace_id=None must fall back to the
        # privacy-safe canonical_session, never the raw repo_root.
        rec = _record(
            repo_root="/workspace/project-alpha",
            workspace=SimpleNamespace(
                workspace_id=None, canonical_session="mozyo-project-alpha-abcdef"
            ),
        )
        obs, _ = self._project([rec])
        self.assertEqual("mozyo-project-alpha-abcdef", obs[0].workspace_id)
        self.assertNotIn("/workspace", obs[0].workspace_id)

    def test_lane_resolved_once_per_repo_root(self) -> None:
        obs, calls = self._project(
            [_record(pane_id="%1"), _record(pane_id="%2", agent_kind="claude")]
        )
        self.assertEqual(2, len(obs))
        self.assertEqual([("/workspace/project-alpha", "wsX")], calls)


class CoexistingObservationsUseCaseTest(unittest.TestCase):
    def test_inventory_failure_degrades_to_empty(self) -> None:
        ops = FakeAdoptOps(inventory_error=RuntimeError("no tmux"))
        self.assertEqual(
            [], CockpitAdoptUseCase(ops).coexisting_normal_observations("mozyo-cockpit")
        )

    def test_projects_snapshot_records_through_port_lane_resolution(self) -> None:
        ops = FakeAdoptOps(snapshot=SimpleNamespace(records=(_record(),)))
        obs = CockpitAdoptUseCase(ops).coexisting_normal_observations("mozyo-cockpit")
        self.assertEqual(1, len(obs))
        self.assertEqual([("/workspace/project-alpha", "wsX")], ops.lane_calls)


class AdoptAdvisoryUseCaseTest(unittest.TestCase):
    def test_detects_candidate_from_projected_inventory(self) -> None:
        records = (
            _record(pane_id="%2"),
            _record(pane_id="%3", agent_kind="claude"),
        )
        ops = FakeAdoptOps(snapshot=SimpleNamespace(records=records))
        advisory = CockpitAdoptUseCase(ops).adopt_advisory(_ws(), "mozyo-cockpit")
        self.assertEqual(ADOPT_STATUS_CANDIDATE, advisory.status)
        self.assertEqual("mozyo-ws", advisory.candidates[0].session)

    def test_projection_failure_degrades_to_benign_none_advisory(self) -> None:
        # A lane-resolution error escapes the observation projection (only the
        # inventory read is tolerated there) and is caught by the advisory's
        # outer tolerance — the original nested-tolerance shape.
        ops = FakeAdoptOps(
            snapshot=SimpleNamespace(records=(_record(),)),
            lane_error=RuntimeError("registry down"),
        )
        advisory = CockpitAdoptUseCase(ops).adopt_advisory(_ws(), "mozyo-cockpit")
        self.assertEqual(ADOPT_STATUS_NONE, advisory.status)
        self.assertEqual("wsX", advisory.workspace_id)


class ResolveAdoptUseCaseTest(unittest.TestCase):
    def _resolve(self, ops, advisory, **over):
        base = dict(
            columns=[], session_present=True, already_in_cockpit=False,
            existing_codex=[{"pane_id": "%9", "pane_left": 0, "pane_width": 100}],
            advisory=advisory,
        )
        base.update(over)
        return CockpitAdoptUseCase(ops).resolve_adopt(_ws(), "mozyo-cockpit", **base)

    def test_already_in_cockpit_blocks(self) -> None:
        plan, blocked, clients = self._resolve(
            FakeAdoptOps(), _candidate_advisory(), already_in_cockpit=True
        )
        self.assertIsNone(plan)
        self.assertIn("already a cockpit column", blocked)
        self.assertEqual((), clients)

    def test_non_candidate_advisory_blocks(self) -> None:
        plan, blocked, _ = self._resolve(FakeAdoptOps(), _none_advisory())
        self.assertIsNone(plan)
        self.assertTrue(blocked)

    def test_missing_cockpit_session_blocks(self) -> None:
        plan, blocked, _ = self._resolve(
            FakeAdoptOps(), _candidate_advisory(), session_present=False, columns=None
        )
        self.assertIsNone(plan)
        self.assertIn("does not exist yet", blocked)

    def test_attached_source_client_fails_closed(self) -> None:
        plan, blocked, clients = self._resolve(
            FakeAdoptOps(attached=("/dev/ttys003",)), _candidate_advisory()
        )
        self.assertIsNone(plan)
        self.assertIn("attached client", blocked)
        self.assertEqual(("/dev/ttys003",), clients)

    def test_missing_codex_anchor_blocks(self) -> None:
        plan, blocked, _ = self._resolve(
            FakeAdoptOps(anchor=None), _candidate_advisory()
        )
        self.assertIsNone(plan)
        self.assertIn("no mozyo-identified", blocked)

    def test_clean_candidate_plans_next_column(self) -> None:
        plan, blocked, clients = self._resolve(FakeAdoptOps(), _candidate_advisory())
        self.assertIsNone(blocked)
        self.assertEqual((), clients)
        self.assertEqual("mozyo-ws", plan.source_session)
        self.assertEqual("%2", plan.source_codex_pane)
        self.assertEqual("%3", plan.source_claude_pane)
        self.assertEqual(1, plan.column_index)  # len(existing_codex)


class HandleAdoptUseCaseTest(unittest.TestCase):
    def _handle(self, ops, *, confirm=False, json_output=False, dry_run=False,
                advisory=None, session_present=True):
        ops.advisory = advisory if advisory is not None else _candidate_advisory()
        args = SimpleNamespace(
            confirm=confirm, json_output=json_output, dry_run=dry_run, codex_ratio=70
        )
        rc = CockpitAdoptUseCase(ops).handle(
            args, _ws(), "mozyo-cockpit",
            columns=[] if session_present else None,
            session_present=session_present,
            already_in_cockpit=False,
            existing_codex=[{"pane_id": "%9", "pane_left": 0, "pane_width": 100}],
        )
        return rc, ops

    def test_json_is_single_parseable_preview_document(self) -> None:
        rc, ops = self._handle(FakeAdoptOps(), confirm=True, json_output=True)
        self.assertEqual(0, rc)
        self.assertEqual(1, len(ops.emitted))
        payload = json.loads(ops.emitted[0])
        self.assertFalse(payload["executes"])
        self.assertTrue(payload["would_execute"])
        # json is a preview surface: no tmux gate, no move.
        self.assertEqual(0, ops.require_tmux_calls)
        self.assertEqual([], ops.executed)

    def test_bare_preview_shows_plan_and_never_moves(self) -> None:
        rc, ops = self._handle(FakeAdoptOps())
        self.assertEqual(0, rc)
        text = "\n".join(ops.emitted)
        self.assertIn("preview; no panes moved", text)
        self.assertIn("run `mozyo cockpit adopt --confirm` to execute this move.", text)
        self.assertEqual([], ops.executed)

    def test_dry_run_outranks_confirm(self) -> None:
        rc, ops = self._handle(FakeAdoptOps(), confirm=True, dry_run=True)
        self.assertEqual(0, rc)
        self.assertIn("preview; no panes moved", "\n".join(ops.emitted))
        self.assertEqual([], ops.executed)
        self.assertEqual(0, ops.require_tmux_calls)

    def test_confirm_executes_and_reports_cleanup(self) -> None:
        rc, ops = self._handle(FakeAdoptOps(), confirm=True)
        self.assertEqual(0, rc)
        self.assertEqual(1, ops.require_tmux_calls)
        self.assertEqual(1, len(ops.executed))
        text = "\n".join(ops.emitted)
        self.assertIn("cockpit adopt: moving normal session 'mozyo-ws'", text)
        self.assertIn("cleanup:mozyo-ws", text)

    def test_confirm_without_plan_dies_fail_closed(self) -> None:
        ops = FakeAdoptOps()
        with self.assertRaises(SystemExit):
            self._handle(ops, confirm=True, advisory=_none_advisory())
        self.assertEqual(1, len(ops.died))
        self.assertEqual([], ops.executed)


if __name__ == "__main__":
    unittest.main()
