"""Redmine #15702 — a fresh lane tab keeps NO empty root shell pane.

The measured live defect: every ``sublane create`` minted its lane tab and then split
agent panes BESIDE the tab's born root pane, so after #15227 removed the root close
(``pane close <locator>`` cannot atomically condition on terminal generation) each lane
tab kept a permanent empty shell column beside its gateway/worker pair.

The fix is occupation, not a reintroduced close: the tab is created with the FIRST
launch slot's ``--cwd`` / ``--env`` (measured herdr 0.8.0: both reach the root shell and
``tab_created`` returns the root's full identity including ``terminal_id``), the first
launch starts IN that born root under the same exact-identity checks a split pane gets,
and only the second slot issues a ``pane split``. Nothing is closed anywhere.

These pins hold the four contract edges:

1. fresh lane create → zero empty pane in the lane tab, zero ``pane close``;
2. default lane / workspace base pane → occupied too since Redmine #15705 moved
   #15702's original scope boundary (the pin tracks the successor contract);
3. heal into an existing tab → no tab create, no occupation;
4. a mislocated / identity-incomplete tab root fails closed before any agent start.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from tests.support.herdr_fake import FakeHerdr
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_session_start import (  # noqa: E501
    _Herdr,
    _launch_env,
)

ISSUE = "15702"


def _prepare(tmp, *, herdr, providers, lane, rows=None, runner=None):
    """Run the real ``prepare_session`` against a fake herdr in an isolated home."""
    repo = Path(tmp) / "repo"
    repo.mkdir()
    home = Path(tmp) / "home"
    home.mkdir()
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
        register_workspace(repo, home=home)
        workspace_id = read_anchor(repo)["workspace_id"]
        if rows is not None:
            herdr.existing_rows = rows(workspace_id)
        result = prepare_session(
            repo_root=repo,
            providers=providers,
            lane_id=lane,
            env=_launch_env(binpath),
            runner=runner or herdr.run,
        )
    return result, workspace_id


class LaneTabRootOccupationTest(unittest.TestCase):
    """The lane-create edge: the born tab root becomes the first agent pane."""

    def test_fresh_lane_leaves_no_empty_pane_and_closes_nothing(self) -> None:
        # `wZ:t2` keeps the lane tab distinct from the fake's workspace default tab,
        # so "every lane-tab pane carries an agent" is a real assertion.
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t2")
        with tempfile.TemporaryDirectory() as tmp:
            result, ws = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
            )
        # The first launch occupies the born root; only the second slot splits.
        self.assertEqual(len(herdr.pane_splits), 1)
        first_start = herdr.start_argvs[0]
        self.assertEqual(first_start[first_start.index("--pane") + 1], "wZ:t2-root")
        # Every pane in the lane tab carries an agent: root (occupied) + one split.
        lane_tab_panes = {
            pane
            for pane, (_, tab, _) in herdr.pane_locations.items()
            if tab == "wZ:t2"
        }
        agent_panes = {
            argv[argv.index("--pane") + 1] for argv in herdr.start_argvs
        }
        self.assertEqual(lane_tab_panes, agent_panes)
        # Occupation, never destruction — and the outcome is typed.
        self.assertEqual(herdr.pane_closes, [])
        self.assertTrue(result.tab_pane_reclaimed)
        self.assertEqual(result.tab_pane_detail, "root_occupied_by_first_launch")
        # The tab create carried the first slot's identity env and worktree cwd.
        tab_create = herdr.tab_creates[0]
        self.assertIn("--cwd", tab_create)
        env_entries = [
            tab_create[i + 1]
            for i, token in enumerate(tab_create)
            if token == "--env"
        ]
        self.assertIn(f"MOZYO_WORKSPACE_ID={ws}", env_entries)
        self.assertIn("MOZYO_AGENT_ROLE=codex", env_entries)
        self.assertIn("MOZYO_LANE_ID=lane-1", env_entries)

    def test_single_provider_lane_occupies_without_any_split(self) -> None:
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp, herdr=herdr, providers=["claude"], lane="lane-1"
            )
        self.assertEqual(herdr.pane_splits, [])
        self.assertEqual(
            herdr.start_argvs[0][herdr.start_argvs[0].index("--pane") + 1],
            "wZ:t1-root",
        )
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.tab_pane_detail, "root_occupied_by_first_launch")

    def test_default_lane_mint_is_occupied_since_15705(self) -> None:
        # This pin originally froze #15702's scope boundary: the default pair kept
        # splitting beside its preserved workspace root. Redmine #15705 moves that
        # boundary — a fresh DEFAULT-lane workspace mint is now first-slot-prepared
        # too, so the pair is root (occupied) + ONE split, still with zero close.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane=""
            )
        self.assertEqual(herdr.tab_creates, [])
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual(herdr.pane_splits[0][2], "wZ:p1")
        self.assertEqual(herdr.pane_closes, [])
        self.assertTrue(result.base_pane_reclaimed)
        self.assertEqual(
            result.base_pane_detail, "root_occupied_by_first_launch"
        )

    def test_heal_into_existing_tab_never_occupies(self) -> None:
        # A heal rejoins the live sibling's tab: no tab create, no occupation, the
        # relaunched slot splits beside the sibling exactly as before.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-1",
                rows=lambda ws: [
                    {
                        "name": encode_assigned_name(ws, "codex", "lane-1"),
                        "pane_id": "w5:pC",
                        "tab_id": "w5:t3",
                    }
                ],
            )
        self.assertEqual(herdr.tab_creates, [])
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual(herdr.pane_splits[0][2], "w5:pC")
        self.assertEqual(result.tab_pane_id, "")
        self.assertFalse(result.tab_pane_reclaimed)

    def test_mislocated_tab_root_is_refused_before_agent_start(self) -> None:
        # A parseable tab root whose identity is a FOREIGN workspace: the occupying
        # slot records the exact pane and refuses agent start (the same
        # record-then-refuse parity a mislocated `pane split` has).
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")

        def foreign_tab_root(argv, *args, **kwargs):
            completed = herdr.run(argv, *args, **kwargs)
            if argv[1:3] != ["tab", "create"] or completed.returncode != 0:
                return completed
            payload = json.loads(completed.stdout)
            payload["result"]["tab"]["tab_id"] = "wF:t1"
            payload["result"]["root_pane"] = {
                "pane_id": "wF:p9",
                "workspace_id": "wF",
                "tab_id": "wF:t1",
                "terminal_id": "terminal:wF:p9",
            }
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError) as caught:
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-1",
                    runner=foreign_tab_root,
                )
        self.assertIn("outside the requested workspace", str(caught.exception))
        self.assertEqual(herdr.start_argvs, [])
        self.assertEqual(herdr.pane_closes, [])

    def test_tab_root_without_terminal_identity_fails_closed(self) -> None:
        # A root the run cannot terminal-bind must never become a prepared launch
        # pane: fail closed before any pane run / agent start.
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")

        def terminal_free_root(argv, *args, **kwargs):
            completed = herdr.run(argv, *args, **kwargs)
            if argv[1:3] != ["tab", "create"] or completed.returncode != 0:
                return completed
            payload = json.loads(completed.stdout)
            payload["result"]["root_pane"].pop("terminal_id", None)
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError) as caught:
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-1",
                    runner=terminal_free_root,
                )
        self.assertIn(
            "no parseable prepared root pane identity", str(caught.exception)
        )
        self.assertEqual(herdr.pane_runs, [])
        self.assertEqual(herdr.start_argvs, [])


class TabRootIdentityExplicitOnlyTest(unittest.TestCase):
    """Review j#107914 finding_1: the root pane's container identity is explicit-only.

    Backfilling a missing ``root_pane.workspace_id`` from the locator prefix or a
    missing ``root_pane.tab_id`` from the envelope made the coherence checks
    trivially true for exactly the payloads they exist to reject. Every field is
    required; a missing OR mismatched field is malformed and yields ``None``.
    """

    @staticmethod
    def _payload(**root):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
            _parse_tab_created_prepared,
        )

        base = {"pane_id": "w1:p4", "terminal_id": "term_x"}
        base.update(root)
        return _parse_tab_created_prepared(
            json.dumps(
                {
                    "result": {
                        "type": "tab_created",
                        "tab": {"tab_id": "w1:t4"},
                        "root_pane": base,
                    }
                }
            )
        )

    def test_complete_identity_is_accepted(self) -> None:
        parsed = self._payload(workspace_id="w1", tab_id="w1:t4")
        self.assertIsNotNone(parsed)
        tab_id, prepared = parsed
        self.assertEqual(tab_id, "w1:t4")
        self.assertEqual(
            (prepared.locator, prepared.workspace_id, prepared.tab_id),
            ("w1:p4", "w1", "w1:t4"),
        )

    def test_missing_workspace_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(tab_id="w1:t4"))

    def test_missing_tab_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w1"))

    def test_missing_both_container_ids_is_rejected(self) -> None:
        self.assertIsNone(self._payload())

    def test_mismatched_workspace_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w9", tab_id="w1:t4"))

    def test_mismatched_tab_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w1", tab_id="w1:t9"))


class LaneTabTopologyPinTest(unittest.TestCase):
    """Full-topology pin through the shared FakeHerdr: N lanes → zero empty tab panes."""

    def test_two_lanes_leave_only_the_host_root_unoccupied(self) -> None:
        fake = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            env = _launch_env(binpath)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                a = prepare_session(
                    repo_root=repo, providers=["codex", "claude"],
                    lane_id="lane-a", env=env, runner=fake.run,
                )
                b = prepare_session(
                    repo_root=repo, providers=["codex", "claude"],
                    lane_id="lane-b", env=env, runner=fake.run,
                )
        host = a.herdr_workspace_id
        self.assertEqual(host, b.herdr_workspace_id)
        agent_panes = {agent["pane_id"] for agent in fake.agents}
        self.assertEqual(len(agent_panes), 4)
        # The ONLY non-agent pane is the host workspace's own cosmetic root; every
        # lane tab holds exactly its pair, with no empty shell column.
        self.assertEqual(set(fake.panes_of(host)) - agent_panes, {f"{host}:p1"})
        for lane_result in (a, b):
            tab_panes = {
                pane
                for pane in fake.panes_of(host)
                if fake.tab_of(pane) == lane_result.herdr_tab_id
            }
            self.assertEqual(tab_panes, tab_panes & agent_panes)
            self.assertEqual(len(tab_panes), 2)


if __name__ == "__main__":
    unittest.main()
