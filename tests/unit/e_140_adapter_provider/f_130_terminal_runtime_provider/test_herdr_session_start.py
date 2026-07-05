"""herdr session-start command tests (Redmine #13261).

Drives the durable-name write side through an injected subprocess ``runner`` and a
real (temp) workspace registration — no live herdr binary. Covers launch+rename,
idempotent adopt, duplicate fail-closed, dry-run planning, unknown provider, an
unconfigured binary, and self-identity env injection into the launched agent.
"""

from __future__ import annotations

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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SLOT_PLANNED,
    HerdrSessionStartError,
    prepare_session,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


class _Herdr:
    """A fake herdr CLI keyed on argv; records launch env for assertions."""

    def __init__(self, *, existing_rows=None, start_locator="w1:pNEW"):
        self.existing_rows = existing_rows or []
        self.start_locator = start_locator
        self.calls: list = []
        self.launch_envs: list = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        self.calls.append(rest)
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.existing_rows}), stderr=""
            )
        if rest[:2] == ["agent", "start"]:
            self.launch_envs.append(env)
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"pane_id": self.start_locator}), stderr=""
            )
        if rest[:2] == ["agent", "rename"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected herdr call: {argv!r}")


class SessionStartTest(unittest.TestCase):
    def _prepare(self, tmp, *, providers, herdr, lane="lane-1", dry_run=False):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = {HERDR_ENV: str(binpath)}
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            result = prepare_session(
                repo_root=repo,
                providers=providers,
                lane_id=lane,
                env=env,
                runner=herdr.run,
                dry_run=dry_run,
            )
            anchor = read_anchor(repo)
        return result, anchor, repo

    def test_launch_and_rename_mints_names(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr
            )
            ws = anchor["workspace_id"]
            self.assertEqual(result.workspace_id, ws)
            outcomes = {s.provider: s.outcome for s in result.slots}
            self.assertEqual(outcomes, {"claude": SLOT_LAUNCHED, "codex": SLOT_LAUNCHED})
            names = {s.provider: s.assigned_name for s in result.slots}
            self.assertEqual(names["claude"], encode_assigned_name(ws, "claude", "lane-1"))
            # A rename was issued for each launched agent.
            renames = [c for c in herdr.calls if c[:2] == ["agent", "rename"]]
            self.assertEqual(len(renames), 2)

    def test_launch_injects_self_identity_env(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude"], herdr=herdr
            )
            ws = anchor["workspace_id"]
        self.assertEqual(len(herdr.launch_envs), 1)
        launch_env = herdr.launch_envs[0]
        self.assertEqual(launch_env["MOZYO_WORKSPACE_ID"], ws)
        self.assertEqual(launch_env["MOZYO_AGENT_ROLE"], "claude")
        self.assertEqual(launch_env["MOZYO_LANE_ID"], "lane-1")
        # cwd pinned to the repo root in the launch argv.
        start = [c for c in herdr.calls if c[:2] == ["agent", "start"]][0]
        self.assertIn("--cwd", start)
        self.assertIn(str(repo), start)

    def test_existing_name_is_adopted_not_relaunched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Pre-seed the registry so we know the workspace_id, then place a live
            # agent already carrying the codex slot's durable name.
            from mozyo_bridge.core.state.workspace_registry import register_workspace

            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                existing = [
                    {
                        "name": encode_assigned_name(ws, "codex", "lane-1"),
                        "pane_id": "w1:pOLD",
                    }
                ]
                herdr = _Herdr(existing_rows=existing)
                binpath = Path(tmp) / "fake-herdr"
                binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
                result = prepare_session(
                    repo_root=repo,
                    providers=["codex"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        slot = result.slots[0]
        self.assertEqual(slot.outcome, SLOT_ADOPTED)
        self.assertEqual(slot.locator, "w1:pOLD")
        # No launch / rename occurred.
        self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "start"]])
        self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "rename"]])

    def test_duplicate_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from mozyo_bridge.core.state.workspace_registry import register_workspace

            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                name = encode_assigned_name(ws, "codex", "lane-1")
                herdr = _Herdr(
                    existing_rows=[
                        {"name": name, "pane_id": "w1:pA"},
                        {"name": name, "pane_id": "w1:pB"},
                    ]
                )
                binpath = Path(tmp) / "fake-herdr"
                binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo,
                        providers=["codex"],
                        lane_id="lane-1",
                        env={HERDR_ENV: str(binpath)},
                        runner=herdr.run,
                    )

    def test_dry_run_plans_without_side_effects(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude"], herdr=herdr, dry_run=True
            )
        self.assertEqual(result.slots[0].outcome, SLOT_PLANNED)
        self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "start"]])

    def test_unknown_provider_fails_closed(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                self._prepare(tmp, providers=["grok"], herdr=herdr)

    def test_unconfigured_binary_fails_closed(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo,
                        providers=["claude"],
                        lane_id="lane-1",
                        env={},  # no MOZYO_HERDR_BINARY
                        runner=herdr.run,
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
