"""Cockpit adopt Phase 1 — detect co-existing normal session + advisory (#11897).

Redmine #11816 j#57823 split adopt into a non-destructive Phase 1 (this US) and
an explicit, confirm-gated Phase 2 (#11898). Phase 1 only *detects* a co-existing
normal `mozyo` session for the current workspace+lane and advises that it is an
adopt candidate — it moves no panes. These tests pin the pure detector, the
inventory projection that feeds it, and the `mozyo cockpit adopt` / advisory
surfaces, all hermetic (no live tmux). Synthetic, neutral identifiers only — no
home paths or private hosts.
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
    ADOPT_STATUS_AMBIGUOUS,
    ADOPT_STATUS_CANDIDATE,
    ADOPT_STATUS_NONE,
    ADOPT_STATUS_PARTIAL,
    NormalSessionObservation,
    detect_adopt_candidates,
)


def _obs(session, role, *, workspace_id="wsX", lane_id="default", pane_id="%1"):
    return NormalSessionObservation(
        session=session,
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        pane_id=pane_id,
    )


class DetectAdoptCandidatesTest(unittest.TestCase):
    def _detect(self, observations, *, workspace_id="wsX", lane_id="default"):
        return detect_adopt_candidates(
            workspace_id=workspace_id,
            lane_id=lane_id,
            observations=observations,
            cockpit_session="mozyo-cockpit",
        )

    def test_none_when_no_matching_observation(self) -> None:
        advisory = self._detect([])
        self.assertEqual(ADOPT_STATUS_NONE, advisory.status)
        self.assertFalse(advisory.adoptable)
        self.assertFalse(advisory.has_candidates)
        self.assertIsNone(advisory.message)

    def test_candidate_when_one_session_has_both_roles(self) -> None:
        advisory = self._detect(
            [
                _obs("mozyo-ws", "codex", pane_id="%2"),
                _obs("mozyo-ws", "claude", pane_id="%3"),
            ]
        )
        self.assertEqual(ADOPT_STATUS_CANDIDATE, advisory.status)
        self.assertTrue(advisory.adoptable)
        self.assertEqual(1, len(advisory.candidates))
        candidate = advisory.candidates[0]
        self.assertEqual("mozyo-ws", candidate.session)
        self.assertEqual(("claude", "codex"), candidate.roles)
        self.assertEqual(("%2", "%3"), candidate.pane_ids)
        self.assertIn("adopt candidate", advisory.message)
        # Honest about Phase 2: the advisory never claims panes get moved here.
        self.assertIn("#11898", advisory.message)

    def test_partial_when_only_one_role_present(self) -> None:
        advisory = self._detect([_obs("mozyo-ws", "codex")])
        self.assertEqual(ADOPT_STATUS_PARTIAL, advisory.status)
        self.assertFalse(advisory.adoptable)  # fail-closed on a half session
        self.assertIn("only codex", advisory.message)

    def test_ambiguous_when_multiple_matching_sessions(self) -> None:
        advisory = self._detect(
            [
                _obs("mozyo-ws-a", "codex"),
                _obs("mozyo-ws-a", "claude"),
                _obs("mozyo-ws-b", "codex"),
                _obs("mozyo-ws-b", "claude"),
            ]
        )
        self.assertEqual(ADOPT_STATUS_AMBIGUOUS, advisory.status)
        self.assertFalse(advisory.adoptable)  # fail-closed, never guess
        self.assertEqual(2, len(advisory.candidates))
        self.assertIn("ambiguous", advisory.message)

    def test_excludes_the_cockpit_session(self) -> None:
        # A cockpit column for the same workspace must never look like an adopt
        # source — its session is the cockpit session and is filtered out.
        advisory = self._detect(
            [
                _obs("mozyo-cockpit", "codex"),
                _obs("mozyo-cockpit", "claude"),
            ]
        )
        self.assertEqual(ADOPT_STATUS_NONE, advisory.status)

    def test_excludes_other_workspace(self) -> None:
        advisory = self._detect(
            [
                _obs("mozyo-ws", "codex", workspace_id="wsOTHER"),
                _obs("mozyo-ws", "claude", workspace_id="wsOTHER"),
            ]
        )
        self.assertEqual(ADOPT_STATUS_NONE, advisory.status)

    def test_excludes_other_lane(self) -> None:
        advisory = self._detect(
            [
                _obs("mozyo-ws", "codex", lane_id="lane-deadbeef0000"),
                _obs("mozyo-ws", "claude", lane_id="lane-deadbeef0000"),
            ]
        )
        self.assertEqual(ADOPT_STATUS_NONE, advisory.status)

    def test_empty_lane_normalizes_to_default(self) -> None:
        # A pre-#11820 normal pane carries no lane id; it must match the primary
        # checkout's `default` lane rather than being dropped.
        advisory = self._detect(
            [
                _obs("mozyo-ws", "codex", lane_id=""),
                _obs("mozyo-ws", "claude", lane_id=""),
            ],
            lane_id="default",
        )
        self.assertEqual(ADOPT_STATUS_CANDIDATE, advisory.status)

    def test_as_dict_is_json_safe(self) -> None:
        advisory = self._detect(
            [_obs("mozyo-ws", "codex"), _obs("mozyo-ws", "claude")]
        )
        payload = advisory.as_dict()
        json.dumps(payload)  # must not raise
        self.assertTrue(payload["adoptable"])
        self.assertEqual("mozyo-ws", payload["candidates"][0]["session"])


class CoexistingObservationsProjectionTest(unittest.TestCase):
    """`_coexisting_normal_observations` keeps only normal-`mozyo` agent panes."""

    def _record(self, **over):
        from mozyo_bridge.domain.agent_discovery import ROLE_SOURCE_WINDOW_NAME
        from mozyo_bridge.session_inventory import InventoryRecord, WorkspaceIdentity

        base = dict(
            pane_id="%1",
            session="mozyo-ws",
            window_index="0",
            window_name="codex",
            pane_index="0",
            pane_active=True,
            process="codex",
            cwd="/workspace/project-alpha",
            repo_root="/workspace/project-alpha",
            agent_kind="codex",
            role_source=ROLE_SOURCE_WINDOW_NAME,
            workspace=WorkspaceIdentity(
                workspace_id="wsX",
                canonical_session="mozyo-ws",
                project_name=None,
                source="derivation",
            ),
        )
        base.update(over)
        return InventoryRecord(**base)

    @contextlib.contextmanager
    def _inventory(self, records):
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.cockpit_layout import DEFAULT_LANE, LaneIdentity
        from mozyo_bridge.session_inventory import InventorySnapshot

        snapshot = InventorySnapshot(
            records=tuple(records),
            collected_at=None,
            source="runtime",
            stale=False,
            inventory_path=Path("/workspace/inventory.sqlite"),
        )
        with patch(
            "mozyo_bridge.session_inventory.take_inventory", return_value=snapshot
        ), patch.object(
            commands,
            "_resolve_workspace_lane",
            return_value=LaneIdentity(DEFAULT_LANE, None),
        ):
            yield

    def test_keeps_window_name_normal_agent(self) -> None:
        from mozyo_bridge.application import commands

        with self._inventory([self._record()]):
            obs = commands._coexisting_normal_observations("mozyo-cockpit")
        self.assertEqual(1, len(obs))
        self.assertEqual("mozyo-ws", obs[0].session)
        self.assertEqual("wsX", obs[0].workspace_id)
        self.assertEqual("codex", obs[0].role)

    def test_drops_cockpit_pane_by_role_source(self) -> None:
        # Cockpit panes carry the role on `@mozyo_agent_role`
        # (role_source=pane_option); they are not a normal-session adopt source.
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.agent_discovery import ROLE_SOURCE_PANE_OPTION

        rec = self._record(session="mozyo-cockpit", role_source=ROLE_SOURCE_PANE_OPTION)
        with self._inventory([rec]):
            obs = commands._coexisting_normal_observations("mozyo-cockpit")
        self.assertEqual([], obs)

    def test_drops_unknown_agent_kind(self) -> None:
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.agent_discovery import (
            AGENT_KIND_UNKNOWN,
            ROLE_SOURCE_UNKNOWN,
        )

        rec = self._record(agent_kind=AGENT_KIND_UNKNOWN, role_source=ROLE_SOURCE_UNKNOWN)
        with self._inventory([rec]):
            obs = commands._coexisting_normal_observations("mozyo-cockpit")
        self.assertEqual([], obs)

    def test_tolerant_when_inventory_raises(self) -> None:
        from mozyo_bridge.application import commands

        with patch(
            "mozyo_bridge.session_inventory.take_inventory",
            side_effect=RuntimeError("no tmux"),
        ):
            self.assertEqual([], commands._coexisting_normal_observations("mozyo-cockpit"))


class CockpitAdoptCommandTest(unittest.TestCase):
    """`mozyo cockpit adopt` is detect-only — it reports, never mutates (#11897)."""

    def _args(self, **over):
        base = dict(
            action="adopt", repo="/workspace/project-alpha", codex_ratio=70,
            cockpit_session=None, dry_run=False, json_output=False, no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, columns, advisory):
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.cockpit_layout import DEFAULT_LANE, LaneIdentity

        canon = argparse.Namespace(name="mozyo-ws", workspace_id="wsX")
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_resolve_workspace_lane",
                         return_value=LaneIdentity(DEFAULT_LANE, None)), \
            patch.object(commands, "require_tmux") as require_tmux, \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_cockpit_adopt_advisory", return_value=advisory), \
            patch.object(commands, "session_exists", return_value=bool(columns)), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield require_tmux, run_tmux, execvp

    def _run(self, args, *, columns, advisory):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(columns=columns, advisory=advisory) as (require_tmux, run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cmd_cockpit(args)
        return rc, out.getvalue(), require_tmux, run_tmux, execvp

    def _candidate_advisory(self):
        return detect_adopt_candidates(
            workspace_id="wsX",
            lane_id="default",
            observations=[
                _obs("mozyo-ws", "codex", pane_id="%2"),
                _obs("mozyo-ws", "claude", pane_id="%3"),
            ],
        )

    def test_reports_candidate_and_never_mutates(self) -> None:
        rc, out, require_tmux, run_tmux, execvp = self._run(
            self._args(), columns=None, advisory=self._candidate_advisory()
        )
        self.assertEqual(0, rc)
        self.assertIn("detect-only", out)
        self.assertIn("candidate: session=mozyo-ws", out)
        self.assertIn("#11898", out)
        run_tmux.assert_not_called()  # read-only / non-mutating
        execvp.assert_not_called()  # never attaches / spawns a window
        require_tmux.assert_not_called()  # adopt does not gate on mutable tmux

    def test_json_payload_is_non_mutating(self) -> None:
        rc, out, _rt, run_tmux, _ev = self._run(
            self._args(json_output=True), columns=None, advisory=self._candidate_advisory()
        )
        payload = json.loads(out)
        self.assertEqual("cockpit adopt", payload["command"])
        self.assertEqual(1, payload["phase"])
        self.assertFalse(payload["mutating"])
        self.assertFalse(payload["already_in_cockpit"])
        self.assertTrue(payload["advisory"]["adoptable"])
        run_tmux.assert_not_called()

    def test_already_in_cockpit_reports_focus_priority(self) -> None:
        # The workspace+lane is already a cockpit column -> focus priority, so
        # there is nothing to adopt (j#57823).
        cols = [{"pane_id": "%5", "workspace_id": "wsX", "role": "codex",
                 "lane_id": "default", "pane_left": 0, "pane_width": 80}]
        none_advisory = detect_adopt_candidates(
            workspace_id="wsX", lane_id="default", observations=[]
        )
        rc, out, _rt, run_tmux, _ev = self._run(
            self._args(), columns=cols, advisory=none_advisory
        )
        self.assertEqual(0, rc)
        self.assertIn("already a cockpit column", out)
        run_tmux.assert_not_called()

    def test_no_candidate_reports_clean(self) -> None:
        none_advisory = detect_adopt_candidates(
            workspace_id="wsX", lane_id="default", observations=[]
        )
        rc, out, _rt, run_tmux, _ev = self._run(
            self._args(), columns=None, advisory=none_advisory
        )
        self.assertEqual(0, rc)
        self.assertIn("no co-existing normal", out)
        run_tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
