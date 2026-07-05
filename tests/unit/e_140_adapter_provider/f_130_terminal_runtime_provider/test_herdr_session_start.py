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
    """A fake herdr CLI (0.7.1 shape) keyed on argv; records start argv + env.

    ``agent start`` returns the real herdr 0.7.1 JSON envelope
    (``result.type == "agent_started"``, locator at ``result.agent.pane_id``). There
    is no ``agent rename`` branch — a stray rename call raises (the durable name is
    applied at start).
    """

    def __init__(self, *, existing_rows=None, start_locator="w1:pNEW"):
        self.existing_rows = existing_rows or []
        self.start_locator = start_locator
        self.calls: list = []
        self.launch_envs: list = []
        self.start_argvs: list = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        self.calls.append(rest)
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.existing_rows}), stderr=""
            )
        if rest[:2] == ["agent", "start"]:
            self.launch_envs.append(env)
            self.start_argvs.append(rest)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "id": "cli:agent:start",
                        "result": {
                            "agent": {
                                "name": rest[2],
                                "pane_id": self.start_locator,
                                "agent_status": "unknown",
                            },
                            "argv": rest,
                            "type": "agent_started",
                        },
                    }
                ),
                stderr="",
            )
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

    def test_launch_mints_names_at_start_no_rename(self) -> None:
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
            # The durable name is applied AT START (positional), never via rename.
            self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "rename"]])
            for argv in herdr.start_argvs:
                # argv = ["agent", "start", <NAME>, "--cwd", ...]
                self.assertEqual(argv[2], names[argv[-1]])  # -- <provider> is argv[-1]

    def test_launch_injects_self_identity_via_env_flags(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude"], herdr=herdr
            )
            ws = anchor["workspace_id"]
        # Self-identity rides on --env flags (the client env does NOT reach the
        # server-spawned agent), so assert the --env triplet + name positional +
        # --cwd + --no-focus in the start argv, not the runner env kwarg.
        start = herdr.start_argvs[0]
        self.assertEqual(start[:3], ["agent", "start", encode_assigned_name(ws, "claude", "lane-1")])
        self.assertIn("--cwd", start)
        self.assertIn(str(repo), start)
        self.assertIn("--no-focus", start)
        self.assertIn(f"MOZYO_WORKSPACE_ID={ws}", start)
        self.assertIn("MOZYO_AGENT_ROLE=claude", start)
        self.assertIn("MOZYO_LANE_ID=lane-1", start)
        # `-- <provider>` terminates the argv.
        self.assertEqual(start[-2:], ["--", "claude"])

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

    def test_duplicate_provider_slot_fails_before_side_effect(self) -> None:
        # Redmine #13261 j#72532: a repeated provider is a repeated (provider, lane)
        # slot; it must fail closed BEFORE any launch/rename so the read side never
        # sees two agents minting the same mzb1 name.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo,
                        providers=["claude", "claude"],
                        lane_id="lane-1",
                        env={HERDR_ENV: str(binpath)},
                        runner=herdr.run,
                    )
        # No side effect: not even `agent list` ran (the guard precedes binary
        # resolution, registration, and the inventory snapshot).
        self.assertEqual(herdr.calls, [])


class SessionStartCliTest(unittest.TestCase):
    def test_repeated_agent_flag_dies_fail_closed(self) -> None:
        from mozyo_bridge.application.cli import build_parser

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = build_parser().parse_args(
                ["herdr", "session-start", "--agent", "claude", "--agent", "claude"]
            )
            args.repo = str(repo)
            with self.assertRaises(SystemExit) as ctx:
                args.func(args)
            # Non-zero fail-closed exit (die), not a silent success.
            self.assertNotEqual(ctx.exception.code, 0)

    def test_default_both_providers_still_valid(self) -> None:
        # The default invocation (no --agent) resolves to both providers with no
        # duplicate, so the slot guard does not fire.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            prepare_session as _prepare_session,
        )

        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                result = _prepare_session(
                    repo_root=repo,
                    providers=["claude", "codex"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(
            {s.provider for s in result.slots}, {"claude", "codex"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
