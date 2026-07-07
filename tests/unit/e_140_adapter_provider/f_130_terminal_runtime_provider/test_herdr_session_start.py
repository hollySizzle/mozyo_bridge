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
    derive_lane_workspace_token,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SLOT_PLANNED,
    HerdrSessionStartError,
    herdr_workspace_segment,
    prepare_session,
)

HERDR_ENV = "MOZYO_HERDR_BINARY"


class _Herdr:
    """A fake herdr CLI (0.7.1 shape) keyed on argv; records start argv + env.

    ``agent start`` returns the real herdr 0.7.1 JSON envelope
    (``result.type == "agent_started"``, locator at ``result.agent.pane_id``). There
    is no ``agent rename`` branch — a stray rename call raises (the durable name is
    applied at start).

    ``workspace create`` / ``pane close`` model the Redmine #13330 empty-base-pane
    reclaim: ``workspace create`` returns a ``workspace_created`` envelope carrying a
    ``root_pane.pane_id`` (the empty base pane), and ``pane close`` acknowledges the
    reclaim. ``start_fails`` / ``close_fails`` drive the fail-closed / non-fatal
    branches. Calls are recorded so tests can assert the exact herdr choreography.
    """

    def __init__(
        self,
        *,
        existing_rows=None,
        start_locator=None,
        created_workspace="wZ",
        start_fails=False,
        close_fails=False,
    ):
        self.existing_rows = existing_rows or []
        # By default the launched agent lands in the workspace it was told to via
        # `--workspace` (the realistic herdr behaviour). An explicit `start_locator`
        # overrides that — used to force a mislocated launch (#13330 review j#73231).
        self.start_locator = start_locator
        self.created_workspace = created_workspace
        self.start_fails = start_fails
        self.close_fails = close_fails
        self.calls: list = []
        self.launch_envs: list = []
        self.start_argvs: list = []
        self.workspace_creates: list = []
        self.pane_closes: list = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        rest = list(argv[1:])
        self.calls.append(rest)
        if rest == ["agent", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"agents": self.existing_rows}), stderr=""
            )
        if rest[:2] == ["workspace", "create"]:
            self.workspace_creates.append(rest)
            wid = self.created_workspace
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "id": "cli:workspace:create",
                        "result": {
                            "type": "workspace_created",
                            "workspace": {"workspace_id": wid},
                            "root_pane": {"pane_id": f"{wid}:p1"},
                        },
                    }
                ),
                stderr="",
            )
        if rest[:2] == ["pane", "close"]:
            self.pane_closes.append(rest)
            if self.close_fails:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="pane close refused"
                )
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"result": {"type": "ok"}}), stderr=""
            )
        if rest[:2] == ["agent", "start"]:
            self.launch_envs.append(env)
            self.start_argvs.append(rest)
            if self.start_fails:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="agent start refused"
                )
            if self.start_locator is not None:
                pane_id = self.start_locator
            elif "--workspace" in rest:
                # Land in the requested workspace with a distinct pane per launch.
                wid = rest[rest.index("--workspace") + 1]
                pane_id = f"{wid}:p{len(self.start_argvs) + 1}"
            else:
                pane_id = "w1:pNEW"
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "id": "cli:agent:start",
                        "result": {
                            "agent": {
                                "name": rest[2],
                                "pane_id": pane_id,
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

    def test_launch_injects_herdr_binary_env(self) -> None:
        # Redmine #13331 j#73312 scope addition #1: the launched agent is itself a
        # mozyo operator that runs its own `handoff send`, and herdr resolves its
        # binary only from the trusted env. Injecting the already-resolved binary as
        # `--env MOZYO_HERDR_BINARY=<binary>` removes the inline
        # `MOZYO_HERDR_BINARY=$(command -v herdr)` the coordinator had to prepend.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(tmp, providers=["codex"], herdr=herdr)
            # `_resolve_binary` returns a path-shaped trusted value verbatim (an
            # existing executable), so the injected value is exactly what `_prepare`
            # put in the env — no symlink resolution.
            binpath = str(Path(tmp) / "fake-herdr")
        start = herdr.start_argvs[0]
        self.assertIn(f"MOZYO_HERDR_BINARY={binpath}", start)
        # It rides on an `--env` flag (server-spawned agent path), never widened to a
        # repo-local binary — the value is the launcher's trusted resolved binary.
        idx = start.index(f"MOZYO_HERDR_BINARY={binpath}")
        self.assertEqual(start[idx - 1], "--env")

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
        # A dry-run plans nothing to launch, so no workspace is created and no base
        # pane is reclaimed (Redmine #13330).
        self.assertEqual(herdr.workspace_creates, [])
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.base_pane_id, "")

    def test_cold_start_creates_workspace_launches_with_flag_and_reclaims(self) -> None:
        # Redmine #13330: a pure cold start explicitly creates the workspace, launches
        # every slot into it (`--workspace`), and reclaims ONLY the returned root pane
        # after all launches succeed.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr
            )
        # Exactly one workspace create; each launch carries `--workspace wZ`.
        self.assertEqual(len(herdr.workspace_creates), 1)
        for argv in herdr.start_argvs:
            self.assertIn("--workspace", argv)
            self.assertEqual(argv[argv.index("--workspace") + 1], "wZ")
        # Exactly the created root pane is closed — never a scanned-for shell.
        self.assertEqual(herdr.pane_closes, [["pane", "close", "wZ:p1"]])
        self.assertEqual(result.herdr_workspace_id, "wZ")
        self.assertEqual(result.base_pane_id, "wZ:p1")
        self.assertTrue(result.base_pane_reclaimed)
        self.assertEqual(result.base_pane_detail, "")
        # Every launched agent actually landed inside the created workspace (#13330
        # review j#73231) — not a herdr-auto-created sibling.
        for slot in result.slots:
            self.assertTrue(slot.locator.startswith("wZ:"))
        # Ordering: create BEFORE both launches, close AFTER both launches.
        kinds = [tuple(c[:2]) for c in herdr.calls]
        create_i = kinds.index(("workspace", "create"))
        close_i = kinds.index(("pane", "close"))
        start_is = [i for i, k in enumerate(kinds) if k == ("agent", "start")]
        self.assertTrue(create_i < min(start_is))
        self.assertTrue(close_i > max(start_is))

    def test_all_adopt_makes_no_workspace_and_no_close(self) -> None:
        # Redmine #13330: an all-adopt run launches nothing, so it stays byte-invariant
        # — no workspace create, no base pane, no reclaim.
        with tempfile.TemporaryDirectory() as tmp:
            from mozyo_bridge.core.state.workspace_registry import register_workspace

            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                existing = [
                    {"name": encode_assigned_name(ws, "claude", "lane-1"), "pane_id": "w7:pC"},
                    {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "w7:pX"},
                ]
                herdr = _Herdr(existing_rows=existing)
                result = prepare_session(
                    repo_root=repo,
                    providers=["claude", "codex"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(herdr.workspace_creates, [])
        self.assertEqual(herdr.pane_closes, [])
        self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "start"]])
        self.assertEqual(result.base_pane_id, "")

    def test_launch_failure_leaves_root_pane_unclosed_and_fails_closed(self) -> None:
        # Redmine #13330: a launch failure raises BEFORE reclaim — the created root
        # pane is left as residue (an implementation failure), never closed blindly.
        herdr = _Herdr(start_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                self._prepare(tmp, providers=["claude", "codex"], herdr=herdr)
        # The workspace was created (residue) but the base pane was NOT closed.
        self.assertEqual(len(herdr.workspace_creates), 1)
        self.assertEqual(herdr.pane_closes, [])

    def test_mislocated_launch_fails_closed_and_leaves_base_pane(self) -> None:
        # Redmine #13330 review j#73231: if `agent start` lands in a DIFFERENT
        # workspace than `--workspace` requested (herdr ignored the flag / spec
        # drift), fail closed — never trust it, and never close the created root pane
        # (an auto-created base pane may survive in the other workspace).
        herdr = _Herdr(created_workspace="wZ", start_locator="w9:pBAD")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                self._prepare(tmp, providers=["claude", "codex"], herdr=herdr)
        self.assertEqual(len(herdr.workspace_creates), 1)
        self.assertEqual(herdr.pane_closes, [])

    def test_root_pane_close_failure_is_non_fatal(self) -> None:
        # Redmine #13330: a `pane close` failure is cosmetic residue only — the agents
        # are already live — so it is recorded, not raised.
        herdr = _Herdr(close_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr
            )
        self.assertEqual(herdr.pane_closes, [["pane", "close", "wZ:p1"]])
        self.assertEqual(result.base_pane_id, "wZ:p1")
        self.assertFalse(result.base_pane_reclaimed)
        self.assertTrue(result.base_pane_detail)
        # Slots still launched successfully despite the failed reclaim.
        self.assertTrue(all(s.outcome == SLOT_LAUNCHED for s in result.slots))

    def test_mixed_adopt_launch_reuses_adopted_workspace_no_base_pane(self) -> None:
        # Redmine #13330: when one slot adopts a live agent, launches land in that
        # agent's existing workspace (`--workspace w5`) — no new workspace, no base
        # pane — instead of creating a fresh one.
        with tempfile.TemporaryDirectory() as tmp:
            from mozyo_bridge.core.state.workspace_registry import register_workspace

            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                existing = [
                    {"name": encode_assigned_name(ws, "codex", "lane-1"), "pane_id": "w5:pOLD"},
                ]
                herdr = _Herdr(existing_rows=existing, start_locator="w5:p2")
                result = prepare_session(
                    repo_root=repo,
                    providers=["claude", "codex"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(herdr.workspace_creates, [])
        self.assertEqual(herdr.pane_closes, [])
        self.assertEqual(result.herdr_workspace_id, "w5")
        self.assertEqual(result.base_pane_id, "")
        # The launched claude slot carries `--workspace w5` (the adopted workspace).
        self.assertEqual(len(herdr.start_argvs), 1)
        launch = herdr.start_argvs[0]
        self.assertEqual(launch[launch.index("--workspace") + 1], "w5")

    def test_launch_target_from_adopted_conflicting_prefixes_fail_closed(self) -> None:
        # Redmine #13330 mixed-case gate: adopted agents spanning >1 herdr workspace
        # fail closed rather than guessing a launch target. (Structurally unreachable
        # with the 2-provider set, but the guard stays fail-closed if it grows.)
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            _launch_target_from_adopted,
        )

        self.assertEqual(_launch_target_from_adopted([]), "")
        self.assertEqual(_launch_target_from_adopted(["w5:pA", "w5:pB"]), "w5")
        with self.assertRaises(HerdrSessionStartError):
            _launch_target_from_adopted(["w5:pA", "w6:pB"])

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


class LinkedWorktreeIdentityTest(unittest.TestCase):
    """Redmine #13331 (design j#73357): on a REAL linked git worktree, the lane's mzb1
    `workspace` segment is the path-derived token — not the inherited main registry id
    (#13152) — so `prepare_session` no longer crashes and mint agrees with the shared
    resolver. Scratch standalone repos do NOT reproduce this (they mint a distinct
    registry id), which is exactly why the earlier fake-dir coverage missed j#73348."""

    def _git(self, path, *args):
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        )

    def _init_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init", "-q")
        self._git(path, "config", "user.email", "t@t")
        self._git(path, "config", "user.name", "t")
        (path / "README.md").write_text("x", encoding="utf-8")
        self._git(path, "add", "-A")
        self._git(path, "commit", "-qm", "init")

    def test_prepare_session_on_linked_worktree_uses_derived_token(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)  # a registered main is NOT required for the token
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13331_x")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            # The herdr subprocess calls ride the injected fake runner; `_is_linked_worktree`
            # uses the REAL git against the REAL worktree (subprocess.run is NOT patched).
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                result = prepare_session(
                    repo_root=wt,
                    providers=["codex", "claude"],
                    lane_id="",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
                # The shared resolver agrees with what was minted (mint == resolve).
                segment = herdr_workspace_segment(wt)
            token = derive_lane_workspace_token(str(wt.resolve()))
        self.assertEqual(result.workspace_id, token)
        self.assertEqual(segment, token)
        names = {s.provider: s.assigned_name for s in result.slots}
        self.assertEqual(names["codex"], encode_assigned_name(token, "codex", ""))
        self.assertEqual(names["claude"], encode_assigned_name(token, "claude", ""))
        # The token is not the main checkout's registry identity (isolation restored).
        main_anchor = read_anchor(main)
        if isinstance(main_anchor, dict) and main_anchor.get("workspace_id"):
            self.assertNotEqual(token, main_anchor["workspace_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
