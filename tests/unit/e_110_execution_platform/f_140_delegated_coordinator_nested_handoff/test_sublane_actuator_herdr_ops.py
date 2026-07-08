"""herdr sublane actuation adapter tests (Redmine #13331, option A per-lane workspace).

Drives :class:`HerdrSublaneActuatorOps` through a stateful fake herdr CLI (0.7.1 shape)
and a real (temp) workspace registry — no live herdr, no tmux. Covers the per-lane
workspace stand-up (``append_lane_column`` = ``prepare_session``), the live-inventory
read-back (``read_lane`` mzb1 decode), the presence-based gateway readiness probe, the
cross-workspace dispatch argv, and the backend selector.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SUBLANE_STATE_ACTIVE,
    SUBLANE_STATE_GATEWAY_ONLY,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


class _StatefulHerdr:
    """A fake herdr whose ``agent list`` reflects agents launched via ``agent start``.

    ``workspace create`` mints a fresh ``wL`` workspace with a root pane; ``agent start``
    lands each launch in the requested ``--workspace`` at a distinct pane and records it,
    so a later ``agent list`` returns those live rows (name + pane_id). This is what lets
    the append → read-back round trip resolve the lane from the live inventory.
    """

    def __init__(self, *, created_workspace="wL"):
        self.created_workspace = created_workspace
        self.agents: list[dict] = []  # {"name", "pane_id"}
        self.start_argvs: list[list] = []
        self._pane_seq = 1
        # #13378: rendered pane text served by `agent read`; set to "" to simulate a
        # live-but-still-booting TUI (blank render).
        self.read_text = "codex composer rendered"

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.agents}), stderr=""
            )
        if rest[:2] == ["agent", "read"]:
            pane = rest[2]
            if not any(a["pane_id"] == pane for a in self.agents):
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="agent_not_found"
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {"result": {"read": {"text": self.read_text, "truncated": False}}}
                ),
                stderr="",
            )
        if rest[:2] == ["workspace", "create"]:
            wid = self.created_workspace
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "result": {
                            "type": "workspace_created",
                            "workspace": {"workspace_id": wid},
                            "root_pane": {"pane_id": f"{wid}:p1"},
                        }
                    }
                ),
                stderr="",
            )
        if rest[:2] == ["pane", "close"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"result": {"type": "ok"}}), stderr=""
            )
        if rest[:2] == ["agent", "start"]:
            self.start_argvs.append(rest)
            name = rest[2]
            wid = rest[rest.index("--workspace") + 1] if "--workspace" in rest else "w1"
            self._pane_seq += 1
            pane_id = f"{wid}:p{self._pane_seq}"
            self.agents.append({"name": name, "pane_id": pane_id})
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "result": {
                            "agent": {"name": name, "pane_id": pane_id},
                            "type": "agent_started",
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected herdr call: {argv!r}")


def _fake_binary(tmp: str) -> Path:
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


class HerdrSublaneOpsTest(unittest.TestCase):
    def _ops(self, tmp, herdr, *, lane_label="issue_13331_x", issue="13331"):
        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        coord = Path(tmp) / "coord"
        coord.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        env = {HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}
        ops = HerdrSublaneActuatorOps(
            repo_root=coord,
            lane_label=lane_label,
            issue=issue,
            env=env,
            runner=herdr.run,
        )
        return ops, home

    def test_append_then_read_lane_round_trips(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # A fresh worktree has no herdr workspace yet -> read_lane is absent.
                self.assertIsNone(ops.read_lane(str(worktree)))
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                lane_ws = read_anchor(worktree)["workspace_id"]
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, lane_ws)
        self.assertEqual(view.lane_id, "default")
        # The requested lane identity is echoed (worktree->workspace is the identity).
        self.assertEqual(view.lane_label, "issue_13331_x")
        self.assertEqual(view.issue, "13331")
        self.assertEqual(view.repo_root, str(worktree))
        # Both managed slots resolve to live herdr locators in the lane workspace.
        self.assertTrue(view.gateway_pane and view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane and view.worker_pane.startswith("wL:"))
        self.assertNotEqual(view.gateway_pane, view.worker_pane)
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)

    def test_append_launches_claude_worker_in_auto_permission_mode(self) -> None:
        # Redmine #13360: lane creation is a managed-pane chokepoint, so the lane's
        # Claude worker must launch reproducibly auto (#11925 parity with the tmux
        # `cockpit append` path) — without it every herdr lane worker stalls on its
        # first permission prompt (coordinator-measured, 2026-07-07). Codex never
        # gets the flag.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
        by_provider = {}
        for argv in herdr.start_argvs:
            provider = argv[argv.index("--") + 1]
            by_provider[provider] = argv
        claude = by_provider["claude"]
        idx = claude.index("--permission-mode")
        self.assertEqual(claude[idx + 1], "auto")
        self.assertGreater(idx, claude.index("--"))
        self.assertNotIn("--permission-mode", by_provider["codex"])

    def test_append_upserts_lane_metadata_record(self) -> None:
        # Redmine #13356 j#73386 Q2: the create command boundary records the
        # token↔(lane_label / issue / branch / worktree) display join.
        from mozyo_bridge.core.state.lane_metadata import load_lane_records

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            ops.branch = "issue_13331_x"
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                lane_ws = read_anchor(worktree)["workspace_id"]
                records = load_lane_records(home=home)
        self.assertIn(lane_ws, records)
        record = records[lane_ws]
        self.assertEqual(record.lane_label, "issue_13331_x")
        self.assertEqual(record.issue_id, "13331")
        self.assertEqual(record.branch, "issue_13331_x")
        self.assertEqual(record.worktree_path, str(worktree))
        self.assertEqual(record.source_backend, "herdr")
        self.assertEqual(record.status, "active")

    def test_read_lane_gateway_only(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                # Drop the worker slot from the live inventory (lost worker).
                herdr.agents = [
                    a for a in herdr.agents if "_claude_" not in a["name"]
                ]
                view = ops.read_lane(str(worktree))
        self.assertIsNotNone(view)
        self.assertTrue(view.gateway_pane)
        self.assertIsNone(view.worker_pane)
        self.assertEqual(view.state, SUBLANE_STATE_GATEWAY_ONLY)

    def test_read_lane_ignores_foreign_and_other_workspace_rows(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                # A foreign (non-mzb1) agent and a mzb1 agent in ANOTHER workspace must
                # not be folded into this lane.
                herdr.agents.append({"name": "someones-shell", "pane_id": "wX:p9"})
                herdr.agents.append(
                    {"name": "mzb1_otherZ2Dws_codex_default", "pane_id": "wY:p9"}
                )
                view = ops.read_lane(str(worktree))
        self.assertIsNotNone(view)
        self.assertTrue(view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane.startswith("wL:"))

    def test_probe_gateway_ready_presence(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                self.assertTrue(ops.probe_gateway_ready(view.gateway_pane))
                self.assertFalse(ops.probe_gateway_ready("wL:p999"))
                self.assertFalse(ops.probe_gateway_ready(""))

    def test_probe_gateway_ready_requires_rendered_content(self) -> None:
        # Redmine #13378: a live-but-blank pane (TUI still booting) is NOT ready —
        # the liveness-only probe fired the in-create dispatch into a still-booting
        # composer (the measured #13366 空振り). Rendered content flips it ready.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
                herdr.read_text = "   \n  "
                self.assertFalse(ops.probe_gateway_ready(view.gateway_pane))
                herdr.read_text = "▌ composer"
                self.assertTrue(ops.probe_gateway_ready(view.gateway_pane))

    def test_heal_lane_column_relaunches_only_the_missing_slot(self) -> None:
        # Redmine #13378: the self-heal is append_lane_column again — prepare_session
        # is adopt-or-launch idempotent, so the surviving worker is adopted (workspace
        # pin) and only the vanished gateway slot is relaunched into the SAME workspace.
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, home = self._ops(tmp, herdr)
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.append_lane_column(str(worktree))
                launches_before = len(herdr.start_argvs)
                # The gateway codex slot vanishes (host-level kill, not a pane close).
                herdr.agents = [a for a in herdr.agents if "_codex_" not in a["name"]]
                ops.heal_lane_column(str(worktree))
                view = ops.read_lane(str(worktree))
        self.assertEqual(len(herdr.start_argvs), launches_before + 1)
        relaunch = herdr.start_argvs[-1]
        self.assertEqual(relaunch[relaunch.index("--") + 1], "codex")
        # The relaunch is pinned into the surviving worker's workspace (adopt pin),
        # so no second workspace (and no new base pane) is created.
        self.assertEqual(relaunch[relaunch.index("--workspace") + 1], "wL")
        self.assertIsNotNone(view)
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        self.assertTrue(view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane.startswith("wL:"))

    def test_append_failure_raises_runtime_error(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            # No MOZYO_HERDR_BINARY in the adapter env -> prepare_session fails closed.
            home = Path(tmp) / "home"
            home.mkdir()
            ops = HerdrSublaneActuatorOps(
                repo_root=Path(tmp),
                lane_label="issue_13331_x",
                issue="13331",
                env={"MOZYO_BRIDGE_HOME": str(home)},
                runner=herdr.run,
            )
            worktree = Path(tmp) / "lane-wt"
            worktree.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(RuntimeError):
                    ops.append_lane_column(str(worktree))

    def test_dispatch_argv_is_cross_workspace_herdr_send(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            ops, _ = self._ops(tmp, herdr)
        argv = ops.dispatch_argv(
            issue="13331",
            journal="73320",
            gateway_pane="wL:p2",
            lane_label="issue_13331_x",
            upstream_coordinator="w2:p2",
            target_repo="/path/to/lane-wt",
        )
        self.assertEqual(argv[:2], ["handoff", "send"])
        # cross-workspace: the lane worktree is named explicitly (its anchor workspace is
        # where the #13331 route authority resolves the gateway).
        self.assertIn("--target-repo", argv)
        self.assertEqual(argv[argv.index("--target-repo") + 1], "/path/to/lane-wt")
        # the herdr locator target is NOT a %pane -> rides the herdr rail (#13320).
        self.assertEqual(argv[argv.index("--target") + 1], "wL:p2")
        self.assertFalse(argv[argv.index("--target") + 1].startswith("%"))
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "queue-enter")
        self.assertEqual(argv[argv.index("--role-profile") + 1], "implementation_gateway")
        self.assertIn("lane=issue_13331_x", argv)
        self.assertIn("upstream_coordinator=w2:p2", argv)


class BackendSelectorTest(unittest.TestCase):
    """`sublane start --execute` picks the herdr adapter only under backend: herdr."""

    @staticmethod
    def _repo(tmp, backend):
        repo = Path(tmp) / f"repo-{backend}"
        repo.mkdir()
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            f"version: 1\nterminal_transport:\n  backend: {backend}\n", encoding="utf-8"
        )
        return repo

    def _select(self, repo):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            _resolve_sublane_ops,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        request = SublaneCreateRequest(
            issue="13331",
            lane_label="issue_13331_x",
            branch="issue_13331_x",
            worktree_path=str(repo) + "-wt",
        )
        ns = argparse.Namespace(repo=str(repo))
        return _resolve_sublane_ops(
            ns, repo_root=repo, request=request, quiet_stdout=False
        )

    def test_herdr_backend_selects_herdr_ops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "herdr"))
        self.assertIsInstance(ops, HerdrSublaneActuatorOps)

    def test_tmux_backend_selects_live_ops(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ops = self._select(self._repo(tmp, "tmux"))
        self.assertIsInstance(ops, LiveSublaneActuatorOps)

    def test_missing_config_defaults_to_tmux(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
            LiveSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo-none"
            repo.mkdir()
            ops = self._select(repo)
        self.assertIsInstance(ops, LiveSublaneActuatorOps)


class HerdrLinkedWorktreeRoundTripTest(unittest.TestCase):
    """Redmine #13331 (design j#73357): the `sublane create --execute` defect scenario —
    a REAL linked git worktree. append_lane_column (prepare_session) mints the lane agents
    under the path-derived token, and read_lane resolves them by the SAME token (not the
    empty / inherited-main registry id that made j#73348 crash). Scratch standalone dirs
    (the other tests) do not reproduce this."""

    def _git(self, path, *args):
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        )

    def test_append_then_read_lane_on_real_worktree_uses_token(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            main.mkdir()
            self._git(main, "init", "-q")
            self._git(main, "config", "user.email", "t@t")
            self._git(main, "config", "user.name", "t")
            (main / "README.md").write_text("x", encoding="utf-8")
            self._git(main, "add", "-A")
            self._git(main, "commit", "-qm", "init")
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13331_x")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = _fake_binary(tmp)
            ops = HerdrSublaneActuatorOps(
                repo_root=main,
                lane_label="issue_13331_x",
                issue="13331",
                env={HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)},
                runner=herdr.run,
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                self.assertIsNone(ops.read_lane(str(wt)))  # fresh worktree, no agents yet
                ops.append_lane_column(str(wt))
                view = ops.read_lane(str(wt))
            token = derive_lane_workspace_token(str(wt.resolve()))
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, token)
        self.assertTrue(view.gateway_pane and view.gateway_pane.startswith("wL:"))
        self.assertTrue(view.worker_pane and view.worker_pane.startswith("wL:"))
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)


class HerdrUseCaseIntegrationTest(unittest.TestCase):
    """The pure SublaneActuateUseCase choreography over the herdr adapter (--no-dispatch,
    so the create → append → read-back → confirm legs run without driving a live send)."""

    def _run(self, tmp, herdr, *, dispatch=False):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            SublaneCreateRequest,
        )

        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        coord = Path(tmp) / "coord"  # non-git -> worktree launch is skipped
        coord.mkdir(exist_ok=True)
        worktree = Path(tmp) / "lane-wt"
        worktree.mkdir(exist_ok=True)
        binpath = _fake_binary(tmp)
        ops = HerdrSublaneActuatorOps(
            repo_root=coord,
            lane_label="issue_13331_lane",
            issue="13331",
            env={HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)},
            runner=herdr.run,
        )
        request = SublaneCreateRequest(
            issue="13331",
            lane_label="issue_13331_lane",
            branch="issue_13331_lane",
            worktree_path=str(worktree),
            journal="73320",
        )
        use_case = SublaneActuateUseCase(ops, gateway_ready_probes=0)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            return use_case.run(
                request, execute=True, dispatch=dispatch, target_repo=str(worktree)
            )

    def test_execute_no_dispatch_stands_up_lane(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self._run(tmp, herdr)
        self.assertFalse(outcome.is_blocked, msg=outcome.reason)
        self.assertTrue(outcome.gateway_pane and outcome.gateway_pane.startswith("wL:"))
        self.assertTrue(outcome.worker_pane and outcome.worker_pane.startswith("wL:"))
        self.assertFalse(outcome.adopted)

    def test_second_run_adopts_existing_lane(self) -> None:
        herdr = _StatefulHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            first = self._run(tmp, herdr)
            self.assertFalse(first.is_blocked, msg=first.reason)
            # Re-run against the SAME tmp: the lane workspace + agents already exist, so
            # read_lane resolves both slots and the use case adopts (no new launch).
            second = self._run(tmp, herdr)
        self.assertFalse(second.is_blocked, msg=second.reason)
        self.assertTrue(second.adopted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
