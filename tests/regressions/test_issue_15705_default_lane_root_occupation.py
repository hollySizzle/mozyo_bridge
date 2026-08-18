"""Redmine #15705 — a fresh DEFAULT-lane workspace keeps NO empty root shell pane.

The owner-reported live defect (the half #15702 deliberately left out of scope):
every bare ``mozyo`` / ``herdr session-start`` cold start minted the coordinator
pair's workspace and then split agent panes BESIDE the workspace's born root pane,
so after #15227 removed the root close (``pane close <locator>`` cannot atomically
condition on terminal generation) each fresh unit kept a permanent empty shell
column beside its coordinator/assistant pair (measured ``w1R:p1`` / ``w1J:p1``).

The fix is the same occupation #15702 proved for lane tabs, applied to the
workspace axis: the workspace is created with the FIRST launch slot's ``--cwd`` /
``--env`` (measured herdr 0.8.0: ``workspace create`` accepts repeatable ``--env``,
both flags reach the root shell, and ``workspace_created`` returns the root's full
identity including ``terminal_id``), the first launch starts IN that born root
under the same exact-identity checks a split pane gets, and only the second slot
issues a ``pane split``. Nothing is closed anywhere.

These pins hold the contract edges:

1. fresh default pair → zero empty pane in the workspace, zero ``pane close``,
   the create carrying the first slot's identity env and worktree cwd;
2. every DEFAULT-lane mint path occupies (per-project, shared_space,
   role_grouped project coordinators) — the sublane HOST mint stays plain
   (its first slot lands in the #15702-occupied lane tab, #13380 cosmetic root);
3. a heal beside a live default-lane sibling mints nothing and occupies nothing;
4. an identity-incoherent / terminal-free workspace root fails closed before any
   pane run or agent start (the j#107914 explicit-only rule, workspace axis).
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
    LaneLaunchContext,
)
from tests.support.herdr_fake import FakeHerdr
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_session_start import (  # noqa: E501
    _Herdr,
    _launch_env,
)

ISSUE = "15705"


def _prepare(
    tmp,
    *,
    herdr,
    providers,
    lane="",
    rows=None,
    runner=None,
    coordinator_placement_mode="per_project_space",
    launch_context=None,
):
    """Run the real ``prepare_session`` against a fake herdr in an isolated home."""
    repo = Path(tmp) / "repo"
    repo.mkdir(exist_ok=True)
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
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
            coordinator_placement_mode=coordinator_placement_mode,
            coordinator_top_workspace_id=(
                workspace_id
                if coordinator_placement_mode == "role_grouped_space"
                else ""
            ),
            launch_context=launch_context,
        )
    return result, workspace_id


def _env_entries(create_argv):
    return [
        create_argv[i + 1]
        for i, token in enumerate(create_argv)
        if token == "--env"
    ]


class DefaultLaneRootOccupationTest(unittest.TestCase):
    """The default-pair edge: the born workspace root becomes the first agent pane."""

    def test_fresh_default_pair_leaves_no_empty_pane_and_closes_nothing(self) -> None:
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, ws = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane=""
            )
        # The first launch occupies the born root; only the second slot splits.
        self.assertEqual(len(herdr.pane_splits), 1)
        first_start = herdr.start_argvs[0]
        self.assertEqual(first_start[first_start.index("--pane") + 1], "wZ:p1")
        # Every pane in the workspace carries an agent: root (occupied) + one split.
        workspace_panes = {
            pane
            for pane, (wid, _tab, _cwd) in herdr.pane_locations.items()
            if wid == "wZ"
        }
        agent_panes = {
            argv[argv.index("--pane") + 1] for argv in herdr.start_argvs
        }
        self.assertEqual(workspace_panes, agent_panes)
        # Occupation, never destruction — and the outcome is typed.
        self.assertEqual(herdr.pane_closes, [])
        self.assertTrue(result.base_pane_reclaimed)
        self.assertEqual(result.base_pane_detail, "root_occupied_by_first_launch")
        # The workspace create carried the first slot's identity env and repo cwd.
        create = herdr.workspace_creates[0]
        self.assertIn("--cwd", create)
        entries = _env_entries(create)
        self.assertIn(f"MOZYO_WORKSPACE_ID={ws}", entries)
        self.assertIn("MOZYO_AGENT_ROLE=codex", entries)
        self.assertIn("MOZYO_LANE_ID=default", entries)

    def test_single_provider_default_lane_occupies_without_any_split(self) -> None:
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(tmp, herdr=herdr, providers=["claude"], lane="")
        self.assertEqual(herdr.pane_splits, [])
        self.assertEqual(
            herdr.start_argvs[0][herdr.start_argvs[0].index("--pane") + 1],
            "wZ:p1",
        )
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.base_pane_detail, "root_occupied_by_first_launch")

    def test_shared_space_coordinators_mint_is_occupied(self) -> None:
        # The #14139 shared coordinators space is a DEFAULT-lane mint too: its born
        # root is occupied by the minting project's first slot; later projects keep
        # joining and splitting beside live panes exactly as before.
        herdr = _Herdr(created_workspace="wS")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="",
                coordinator_placement_mode="shared_space",
            )
        create = herdr.workspace_creates[0]
        self.assertEqual(create[create.index("--label") + 1], "coordinators")
        self.assertTrue(_env_entries(create))
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.base_pane_detail, "root_occupied_by_first_launch")

    def test_role_grouped_project_coordinators_mint_is_occupied(self) -> None:
        # The #14996 shared project-coordinators space: same DEFAULT-lane-shaped
        # mint through the role-grouped resolver, same occupation.
        herdr = _Herdr(created_workspace="wProjects")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="project-accounting",
                coordinator_placement_mode="role_grouped_space",
                launch_context=LaneLaunchContext(lane_kind="delegated_coordinator"),
            )
        create = herdr.workspace_creates[0]
        self.assertEqual(
            create[create.index("--label") + 1], "project-coordinators"
        )
        self.assertTrue(_env_entries(create))
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.base_pane_detail, "root_occupied_by_first_launch")

    def test_sublane_host_mint_stays_plain_and_unoccupied(self) -> None:
        # A non-default lane's first slot lands in its #15702-occupied lane TAB;
        # the host workspace mint must stay the plain (env-free) create and its
        # root the #13380 cosmetic unbound root — preserved, never occupied.
        herdr = _Herdr(created_workspace="wH", created_tab="wH:t2")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
            )
        create = herdr.workspace_creates[0]
        self.assertEqual(_env_entries(create), [])
        self.assertIn("--label", create)
        agent_panes = {
            argv[argv.index("--pane") + 1] for argv in herdr.start_argvs
        }
        self.assertNotIn("wH:p1", agent_panes)
        self.assertFalse(result.base_pane_reclaimed)
        self.assertEqual(
            result.base_pane_detail, "generation_unproven_root_preserved"
        )
        self.assertEqual(result.tab_pane_detail, "root_occupied_by_first_launch")
        self.assertEqual(herdr.pane_closes, [])

    def test_heal_beside_live_default_sibling_mints_and_occupies_nothing(self) -> None:
        # A default-lane heal rejoins the sibling's live workspace: no workspace
        # create, no occupation, the relaunched slot splits beside the sibling.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="",
                rows=lambda ws: [
                    {
                        "name": encode_assigned_name(ws, "codex", ""),
                        "pane_id": "w5:pC",
                    }
                ],
            )
        self.assertEqual(herdr.workspace_creates, [])
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual(herdr.pane_splits[0][2], "w5:pC")
        self.assertEqual(result.base_pane_id, "")
        self.assertFalse(result.base_pane_reclaimed)
        self.assertEqual(herdr.pane_closes, [])

    def test_incoherent_workspace_root_identity_fails_closed(self) -> None:
        # A root pane declaring a FOREIGN container identity cannot become a
        # prepared launch pane: fail closed before any pane run / agent start.
        herdr = _Herdr(created_workspace="wZ")

        def foreign_workspace_root(argv, *args, **kwargs):
            completed = herdr.run(argv, *args, **kwargs)
            if argv[1:3] != ["workspace", "create"] or completed.returncode != 0:
                return completed
            payload = json.loads(completed.stdout)
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
                    lane="",
                    runner=foreign_workspace_root,
                )
        self.assertIn(
            "no parseable prepared root pane identity", str(caught.exception)
        )
        self.assertEqual(herdr.pane_runs, [])
        self.assertEqual(herdr.start_argvs, [])
        self.assertEqual(herdr.pane_closes, [])

    def test_workspace_root_without_terminal_identity_fails_closed(self) -> None:
        # A root the run cannot terminal-bind must never become a prepared launch
        # pane: fail closed before any pane run / agent start.
        herdr = _Herdr(created_workspace="wZ")

        def terminal_free_root(argv, *args, **kwargs):
            completed = herdr.run(argv, *args, **kwargs)
            if argv[1:3] != ["workspace", "create"] or completed.returncode != 0:
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
                    lane="",
                    runner=terminal_free_root,
                )
        self.assertIn(
            "no parseable prepared root pane identity", str(caught.exception)
        )
        self.assertEqual(herdr.pane_runs, [])
        self.assertEqual(herdr.start_argvs, [])


class WorkspaceRootIdentityExplicitOnlyTest(unittest.TestCase):
    """The j#107914 explicit-only rule on the workspace axis.

    The root pane must DECLARE its own workspace_id / tab_id; backfilling a
    missing field from the locator prefix or the envelope workspace would make
    the coherence checks trivially true for exactly the payloads they exist to
    reject. Every field is required; a missing OR mismatched field yields ``None``.
    """

    @staticmethod
    def _payload(**root):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
            _parse_workspace_created_prepared,
        )

        base = {"pane_id": "w1:p1", "terminal_id": "term_x"}
        base.update(root)
        return _parse_workspace_created_prepared(
            json.dumps(
                {
                    "result": {
                        "type": "workspace_created",
                        "workspace": {"workspace_id": "w1"},
                        "root_pane": base,
                    }
                }
            )
        )

    def test_complete_identity_is_accepted(self) -> None:
        parsed = self._payload(workspace_id="w1", tab_id="w1:t1")
        self.assertIsNotNone(parsed)
        workspace_id, prepared = parsed
        self.assertEqual(workspace_id, "w1")
        self.assertEqual(
            (prepared.locator, prepared.workspace_id, prepared.tab_id),
            ("w1:p1", "w1", "w1:t1"),
        )

    def test_missing_workspace_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(tab_id="w1:t1"))

    def test_missing_tab_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w1"))

    def test_missing_both_container_ids_is_rejected(self) -> None:
        self.assertIsNone(self._payload())

    def test_mismatched_workspace_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w9", tab_id="w1:t1"))

    def test_mismatched_tab_id_is_rejected(self) -> None:
        self.assertIsNone(self._payload(workspace_id="w1", tab_id="w9:t1"))


class DefaultLaneTopologyPinTest(unittest.TestCase):
    """Full-topology pin through the shared FakeHerdr: zero non-agent panes."""

    def test_fresh_default_pair_leaves_zero_non_agent_panes(self) -> None:
        fake = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                result = prepare_session(
                    repo_root=repo, providers=["codex", "claude"],
                    lane_id="", env=_launch_env(binpath), runner=fake.run,
                )
        workspace = result.herdr_workspace_id
        agent_panes = {agent["pane_id"] for agent in fake.agents}
        self.assertEqual(len(agent_panes), 2)
        # The pair alone tiles its workspace: the born root is one OF the agent
        # panes, not an empty shell column beside them.
        self.assertEqual(set(fake.panes_of(workspace)), agent_panes)
        self.assertIn(f"{workspace}:p1", agent_panes)


if __name__ == "__main__":
    unittest.main()
