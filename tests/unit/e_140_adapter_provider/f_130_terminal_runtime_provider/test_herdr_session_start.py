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
_TESTS_ROOT = Path(__file__).resolve().parents[3]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from support.herdr_fake import FakeHerdr
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
        created_tab=None,
        tab_bad_payload=False,
        start_fails=False,
        close_fails=False,
    ):
        self.existing_rows = existing_rows or []
        # By default the launched agent lands in the workspace it was told to via
        # `--workspace` (the realistic herdr behaviour). An explicit `start_locator`
        # overrides that — used to force a mislocated launch (#13330 review j#73231).
        self.start_locator = start_locator
        self.created_workspace = created_workspace
        # The tab id `tab create` mints (Redmine #13411); defaults to `<ws>:t1`.
        # ``tab_bad_payload`` returns an unparseable `tab create` payload so the
        # real code fails closed (the tab analogue of a malformed workspace create).
        self.created_tab = created_tab
        self.tab_bad_payload = tab_bad_payload
        self.start_fails = start_fails
        self.close_fails = close_fails
        self.calls: list = []
        self.launch_envs: list = []
        self.start_argvs: list = []
        self.workspace_creates: list = []
        self.tab_creates: list = []
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
        if rest[:2] == ["tab", "create"]:
            self.tab_creates.append(rest)
            wid = rest[rest.index("--workspace") + 1]
            if self.tab_bad_payload:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"result": {"type": "nope"}}), stderr=""
                )
            tab_id = self.created_tab or f"{wid}:t1"
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "id": "cli:tab:create",
                        "result": {
                            "type": "tab_created",
                            "tab": {"tab_id": tab_id},
                            "root_pane": {"pane_id": f"{tab_id}-root"},
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
    def _prepare(
        self,
        tmp,
        *,
        providers,
        herdr,
        lane="lane-1",
        dry_run=False,
        extra_env=None,
        claude_permission_mode_default=None,
        agent_launch=None,
    ):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = {HERDR_ENV: str(binpath)}
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            result = prepare_session(
                repo_root=repo,
                providers=providers,
                lane_id=lane,
                env=env,
                runner=herdr.run,
                dry_run=dry_run,
                claude_permission_mode_default=claude_permission_mode_default,
                agent_launch=agent_launch,
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

    def test_launch_appends_permission_mode_for_claude_with_policy_default(self) -> None:
        # Redmine #13360: the herdr launch chokepoint mirrors the tmux managed-pane
        # `--permission-mode` parity (#11925). A lane-creation caller passing the
        # cockpit/sublane policy default gets a reproducibly-auto Claude worker.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(
                tmp,
                providers=["claude", "codex"],
                herdr=herdr,
                claude_permission_mode_default="auto",
            )
        by_provider = {}
        for argv in herdr.start_argvs:
            provider = argv[argv.index("--") + 1]
            by_provider[provider] = argv
        claude = by_provider["claude"]
        idx = claude.index("--permission-mode")
        self.assertEqual(claude[idx + 1], "auto")
        # The flag rides AFTER `-- claude` so it reaches the claude CLI, not herdr.
        self.assertGreater(idx, claude.index("--"))
        # Codex launches never get the flag (Claude-only policy, #11925 rule 1).
        self.assertNotIn("--permission-mode", by_provider["codex"])

    def test_launch_argv_config_appended_for_sublane_after_permission_mode(self) -> None:
        # Redmine #13425: the config's `launch_argv.{provider}.sublane` tokens reach the
        # herdr launch argv (the #13155 regression fix on the herdr chokepoint). Claude's
        # `--model` is rendered AFTER the managed `--permission-mode` (answer j#73949 Q4);
        # codex gets its own sublane tokens.
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
            AgentLaunchConfig,
        )

        cfg = AgentLaunchConfig.from_record(
            {
                "launch_argv": {
                    "codex": {"sublane": ["--config", "model_reasoning_effort=high"]},
                    "claude": {"sublane": ["--model", "claude-opus-4-8"]},
                }
            }
        )
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(
                tmp,
                providers=["claude", "codex"],
                herdr=herdr,
                lane="issue_x",  # non-default lane -> sublane lane_class
                claude_permission_mode_default="auto",
                agent_launch=cfg,
            )
        by_provider = {}
        for argv in herdr.start_argvs:
            provider = argv[argv.index("--") + 1]
            by_provider[provider] = argv
        claude = by_provider["claude"]
        # `-- claude --permission-mode auto --model claude-opus-4-8` (Q4 order).
        self.assertEqual(
            claude[claude.index("--"):],
            ["--", "claude", "--permission-mode", "auto", "--model", "claude-opus-4-8"],
        )
        codex = by_provider["codex"]
        self.assertEqual(
            codex[codex.index("--"):],
            ["--", "codex", "--config", "model_reasoning_effort=high"],
        )

    def test_launch_argv_config_uses_default_lane_class_for_no_lane(self) -> None:
        # The coordinator pair (no-lane session) is the `default` lane_class, so only the
        # `launch_argv.{provider}.default` tokens apply; the sublane tokens do NOT leak in.
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
            AgentLaunchConfig,
        )

        cfg = AgentLaunchConfig.from_record(
            {
                "launch_argv": {
                    "codex": {
                        "default": ["--config", "model_reasoning_effort=xhigh"],
                        "sublane": ["--config", "model_reasoning_effort=high"],
                    }
                }
            }
        )
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(
                tmp,
                providers=["codex"],
                herdr=herdr,
                lane="",  # no lane -> default lane_class
                agent_launch=cfg,
            )
        codex = herdr.start_argvs[0]
        self.assertEqual(
            codex[codex.index("--"):],
            ["--", "codex", "--config", "model_reasoning_effort=xhigh"],
        )

    def test_launch_argv_none_config_is_byte_invariant(self) -> None:
        # No config (agent_launch=None) appends nothing — byte-for-byte the pre-#13425
        # launch, so an unconfigured launch site is unaffected.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr, agent_launch=None
            )
        for argv in herdr.start_argvs:
            provider = argv[argv.index("--") + 1]
            self.assertEqual(argv[-2:], ["--", provider])

    def test_launch_without_policy_default_is_flagless(self) -> None:
        # No default and no env override: the historical bare `-- claude` launch is
        # byte-invariant (session-start / bare `mozyo` paths pass None, #13360).
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(tmp, providers=["claude"], herdr=herdr)
        start = herdr.start_argvs[0]
        self.assertNotIn("--permission-mode", start)
        self.assertEqual(start[-2:], ["--", "claude"])

    def test_launch_env_override_wins_over_policy_default(self) -> None:
        # MOZYO_CLAUDE_PERMISSION_MODE stays the explicit override rail (#11857):
        # an operator can force any mode (including turning auto OFF with
        # `default`) even when the lane chokepoint passes `auto`.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(
                tmp,
                providers=["claude"],
                herdr=herdr,
                extra_env={"MOZYO_CLAUDE_PERMISSION_MODE": "default"},
                claude_permission_mode_default="auto",
            )
        start = herdr.start_argvs[0]
        idx = start.index("--permission-mode")
        self.assertEqual(start[idx + 1], "default")

    def test_launch_invalid_env_permission_mode_fails_closed(self) -> None:
        # A typo must fail the launch loudly (HerdrSessionStartError), never boot a
        # default-permission agent the operator did not intend (#11857 / #13360).
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError) as ctx:
                self._prepare(
                    tmp,
                    providers=["claude"],
                    herdr=herdr,
                    extra_env={"MOZYO_CLAUDE_PERMISSION_MODE": "yolo"},
                    claude_permission_mode_default="auto",
                )
            self.assertIn("permission mode", str(ctx.exception))
        self.assertFalse(herdr.start_argvs)

    def test_invalid_env_fails_before_any_launch_in_lane_provider_order(self) -> None:
        # Review j#73404: the lane chokepoint requests (codex, claude) in that
        # order. Policy validation must fire BEFORE any side effect — a validation
        # that only ran inside the claude slot's launch left the codex gateway
        # already started (a partial lane) on an invalid env override. Pin: zero
        # `agent start` AND zero `workspace create`.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                self._prepare(
                    tmp,
                    providers=["codex", "claude"],
                    herdr=herdr,
                    extra_env={"MOZYO_CLAUDE_PERMISSION_MODE": "yolo"},
                    claude_permission_mode_default="auto",
                )
        self.assertFalse(herdr.start_argvs)
        self.assertFalse(
            [c for c in herdr.calls if c[:2] == ["workspace", "create"]]
        )

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
        # after all launches succeed. Exercised on the DEFAULT lane so the #13411 tab
        # axis (which adds a tab create + tab root reclaim) never enters — this pins
        # the workspace axis in isolation.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr, lane=""
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
        # are already live — so it is recorded, not raised. Default lane so only the
        # workspace base pane is in play (the #13411 tab axis is pinned separately).
        herdr = _Herdr(close_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr, lane=""
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

    def test_launch_target_for_lane_placement_rules(self) -> None:
        # Redmine #13380 dedicated sublane host workspace: own pins first, then the
        # sibling-lane host EXCLUDING the coordinator's workspace, else create ("").
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            _launch_target_for_lane,
        )

        ws = "wsA"

        def row(role, lane, pane):
            return {"name": encode_assigned_name(ws, role, lane), "pane_id": pane}

        coord = [row("codex", "", "w2:p1"), row("claude", "", "w2:p2")]
        cohabiting = coord + [row("codex", "lane-a", "w2:p5")]
        # 1. own slots pin first — a heal keeps a pair together even inside the
        #    coordinator's workspace (pre-#13380 cohabitation drains via retire).
        self.assertEqual(_launch_target_for_lane(cohabiting, ws, "lane-a", []), "w2")
        # 2. a NEW lane sees only cohabiting/legacy lane pins: the coordinator's
        #    workspace is excluded, so it mints the host ("").
        self.assertEqual(_launch_target_for_lane(cohabiting, ws, "lane-b", []), "")
        # 3. sibling lane slots outside the coordinator's workspace pin the host.
        with_host = coord + [row("codex", "lane-a", "w8:p1")]
        self.assertEqual(_launch_target_for_lane(with_host, ws, "lane-b", []), "w8")
        # 4. the default lane joins only its own pins — never the sublane host.
        self.assertEqual(_launch_target_for_lane(coord, ws, "", []), "w2")
        self.assertEqual(
            _launch_target_for_lane([row("codex", "lane-a", "w8:p1")], ws, "", []), ""
        )
        # 5. rows of ANOTHER mozyo workspace never pin anything.
        foreign = [{"name": encode_assigned_name("wsB", "codex", "lane-a"), "pane_id": "w9:p1"}]
        self.assertEqual(_launch_target_for_lane(foreign, ws, "lane-b", []), "")
        # 6. fail-closed: lane pins outside the coordinator's span two workspaces.
        split_host = coord + [
            row("codex", "lane-a", "w8:p1"),
            row("codex", "lane-c", "w9:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            _launch_target_for_lane(split_host, ws, "lane-b", [])
        # 7. fail-closed: a lane's OWN slots span two workspaces — including via
        #    this run's adopted locators (the #13330 j#73225 mixed-case gate,
        #    subsumed from the retired `_launch_target_from_adopted`).
        split_own = [row("codex", "lane-a", "w8:p1"), row("claude", "lane-a", "w9:p1")]
        with self.assertRaises(HerdrSessionStartError):
            _launch_target_for_lane(split_own, ws, "lane-a", [])
        with self.assertRaises(HerdrSessionStartError):
            _launch_target_for_lane([], ws, "lane-a", ["w5:pA", "w6:pB"])

    def test_lane_cold_start_creates_labelled_host_workspace(self) -> None:
        # Redmine #13380: a lane-slot mint labels the host workspace after the main
        # checkout (cosmetic, operator-readable — never a join key).
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr
            )
        create = herdr.workspace_creates[0]
        self.assertIn("--label", create)
        self.assertEqual(create[create.index("--label") + 1], f"{repo.name}_sublanes")

    def test_default_lane_cold_start_creates_unlabelled_project_workspace(self) -> None:
        # The coordinator pair's project workspace keeps the pre-#13380 argv (no
        # label) — the default-lane path stays byte-invariant.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, anchor, repo = self._prepare(
                tmp, providers=["claude", "codex"], herdr=herdr, lane=""
            )
        create = herdr.workspace_creates[0]
        self.assertNotIn("--label", create)

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

    def _run_cli_with_fake_runner(self, tmp, herdr, *, extra_env=None):
        # Drive the real `herdr session-start` CLI entrypoint (build_parser ->
        # args.func) with the fake herdr injected as the launch runner, so the test
        # observes the exact launched argv the CLI seam produces — not a
        # prepare_session call the CLI might forget to make correctly.
        from mozyo_bridge.application.cli import build_parser
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
            herdr_session_start as hss,
        )

        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
        args = build_parser().parse_args(
            ["herdr", "session-start", "--agent", "claude", "--agent", "codex"]
        )
        args.repo = str(repo)
        real_prepare = hss.prepare_session

        def _prepare_with_fake_runner(**kwargs):
            return real_prepare(runner=herdr.run, **kwargs)

        env = {HERDR_ENV: str(binpath), "MOZYO_BRIDGE_HOME": str(home)}
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env, clear=False), patch.object(
            hss, "prepare_session", _prepare_with_fake_runner
        ):
            # Isolate the override rail (Finding 1, review j#74373): patch.dict merges
            # with clear=False, so an ambient operator/CI MOZYO_CLAUDE_PERMISSION_MODE
            # would leak into the default-path scenario and make it assert the external
            # value instead of the policy default. Remove it unless the scenario sets it
            # explicitly; patch.dict restores the original environ on exit.
            if not (extra_env and "MOZYO_CLAUDE_PERMISSION_MODE" in extra_env):
                os.environ.pop("MOZYO_CLAUDE_PERMISSION_MODE", None)
            rc = args.func(args)
        self.assertEqual(rc, 0)
        by_provider = {}
        for argv in herdr.start_argvs:
            by_provider[argv[argv.index("--") + 1]] = argv
        return by_provider

    def test_cli_session_start_threads_auto_permission_default(self) -> None:
        # Regression #13452 / #13453: the direct `herdr session-start` CLI entrypoint
        # must thread the cockpit/sublane policy default (`auto`) into launch
        # preparation, so a managed Claude relaunched via the runbook command lands
        # `--permission-mode auto` WITHOUT the operator setting
        # MOZYO_CLAUDE_PERMISSION_MODE. Before the fix the CLI called prepare_session()
        # omitting claude_permission_mode_default, so live argv was flagless
        # (`manual mode on`) while `sublane readiness` projected `auto` — the exact
        # projection/live divergence this US closes.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            by_provider = self._run_cli_with_fake_runner(tmp, herdr)
        claude = by_provider["claude"]
        idx = claude.index("--permission-mode")
        self.assertEqual(claude[idx + 1], "auto")
        # The flag rides AFTER `-- claude` so it reaches the claude CLI, not herdr.
        self.assertGreater(idx, claude.index("--"))
        # Codex argv is unchanged (Claude-only policy, #11925 rule 1).
        self.assertNotIn("--permission-mode", by_provider["codex"])

    def test_cli_session_start_env_override_wins_over_policy_default(self) -> None:
        # Contract invariance: hardcoding the `auto` policy default at the CLI seam does
        # NOT usurp the MOZYO_CLAUDE_PERMISSION_MODE override rail (#11857). An operator
        # who exports `default` still gets `--permission-mode default`.
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            by_provider = self._run_cli_with_fake_runner(
                tmp, herdr, extra_env={"MOZYO_CLAUDE_PERMISSION_MODE": "default"}
            )
        claude = by_provider["claude"]
        idx = claude.index("--permission-mode")
        self.assertEqual(claude[idx + 1], "default")
        self.assertNotIn("--permission-mode", by_provider["codex"])


class LinkedWorktreeIdentityTest(unittest.TestCase):
    """Redmine #13377 (design j#73613, shared project workspace): on a REAL linked git
    worktree, the lane's mzb1 `workspace` segment is the MAIN checkout's registry
    identity (#13152 inheritance) and the lane segment is the discriminant — the slots
    are `mzb1_<project-ws>_<role>_<lane_label>` (the #13331 `wt_<hash>` per-lane
    workspace is legacy). Placement refined by #13380 (dedicated sublane host
    workspace): lane slots land in a single sublane host workspace separate from the
    coordinator pair's project workspace — never joining the coordinator's, never one
    per lane. Real git worktrees are used (scratch standalone repos hide the
    inheritance, the j#73348 lesson)."""

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

    def _binpath(self, tmp: Path) -> Path:
        binpath = tmp / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
        return binpath

    def test_prepare_session_on_linked_worktree_uses_project_workspace(self) -> None:
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13377_x")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = self._binpath(Path(tmp))
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                main_ws = read_anchor(main)["workspace_id"]
                # The project's coordinator pair is live in herdr workspace w7: the
                # lane launch must NOT join it (Redmine #13380 dedicated sublane
                # host) — with no live lane slots it mints the labelled host
                # workspace instead.
                herdr = _Herdr(
                    existing_rows=[
                        {
                            "name": encode_assigned_name(main_ws, "codex", ""),
                            "pane_id": "w7:p2",
                        }
                    ],
                    created_workspace="wH",
                )
                result = prepare_session(
                    repo_root=wt,
                    providers=["codex", "claude"],
                    lane_id="issue_13377_x",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
                # The shared resolver agrees with what was minted (mint == resolve):
                # the lane worktree resolves to the MAIN registry identity.
                segment = herdr_workspace_segment(wt)
            token = derive_lane_workspace_token(str(wt.resolve()))
        self.assertEqual(result.workspace_id, main_ws)
        self.assertEqual(segment, main_ws)
        self.assertNotEqual(result.workspace_id, token)  # wt_<hash> is legacy-only
        names = {s.provider: s.assigned_name for s in result.slots}
        self.assertEqual(
            names["codex"], encode_assigned_name(main_ws, "codex", "issue_13377_x")
        )
        self.assertEqual(
            names["claude"], encode_assigned_name(main_ws, "claude", "issue_13377_x")
        )
        # Minted the dedicated sublane host (labelled after the MAIN checkout) and
        # pinned every launch into it — never the coordinator's w7.
        self.assertEqual(len(herdr.workspace_creates), 1)
        create = herdr.workspace_creates[0]
        self.assertIn("--label", create)
        self.assertEqual(create[create.index("--label") + 1], "main_sublanes")
        for argv in herdr.start_argvs:
            self.assertIn("--workspace", argv)
            self.assertEqual(argv[argv.index("--workspace") + 1], "wH")
        self.assertEqual(result.herdr_workspace_id, "wH")
        # Lane=tab (Redmine #13411): a fresh lane also mints a dedicated tab in the
        # host (labelled with the lane key), pins both launches into it, and reclaims
        # BOTH root panes — the host base pane (#13330) and the tab root pane.
        self.assertEqual(len(herdr.tab_creates), 1)
        tab_create = herdr.tab_creates[0]
        self.assertEqual(tab_create[tab_create.index("--workspace") + 1], "wH")
        self.assertEqual(tab_create[tab_create.index("--label") + 1], "issue_13377_x")
        for argv in herdr.start_argvs:
            self.assertEqual(argv[argv.index("--tab") + 1], "wH:t1")
        self.assertEqual(result.herdr_tab_id, "wH:t1")
        self.assertEqual(
            herdr.pane_closes,
            [["pane", "close", "wH:p1"], ["pane", "close", "wH:t1-root"]],
        )

    def test_prepare_session_linked_worktree_joins_live_host_workspace(self) -> None:
        """A second lane joins the sublane host the first lane's slots occupy —
        no new workspace, and never the coordinator's (Redmine #13380)."""
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13380_b")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = self._binpath(Path(tmp))
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                main_ws = read_anchor(main)["workspace_id"]
                herdr = _Herdr(
                    existing_rows=[
                        {
                            "name": encode_assigned_name(main_ws, "codex", ""),
                            "pane_id": "w7:p2",
                        },
                        {
                            "name": encode_assigned_name(
                                main_ws, "codex", "issue_13380_a"
                            ),
                            "pane_id": "w8:p3",
                        },
                    ],
                )
                result = prepare_session(
                    repo_root=wt,
                    providers=["codex", "claude"],
                    lane_id="issue_13380_b",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(herdr.workspace_creates, [])
        self.assertEqual(result.herdr_workspace_id, "w8")
        for argv in herdr.start_argvs:
            self.assertIn("--workspace", argv)
            self.assertEqual(argv[argv.index("--workspace") + 1], "w8")
        # Lane=tab (Redmine #13411): the second lane joins the SAME host workspace
        # w8 (no new workspace) but gets its OWN dedicated tab inside it — the
        # sibling lane's slots (a different lane) never pin this lane's tab. Its
        # tab root pane is the only reclaim (no host base pane — the host already
        # existed).
        self.assertEqual(len(herdr.tab_creates), 1)
        self.assertEqual(
            herdr.tab_creates[0][herdr.tab_creates[0].index("--workspace") + 1], "w8"
        )
        self.assertEqual(result.herdr_tab_id, "w8:t1")
        for argv in herdr.start_argvs:
            self.assertEqual(argv[argv.index("--tab") + 1], "w8:t1")
        self.assertEqual(herdr.pane_closes, [["pane", "close", "w8:t1-root"]])

    def test_prepare_session_linked_worktree_unregistered_main_fails_closed(self) -> None:
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)  # NOT registered
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13377_y")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = self._binpath(Path(tmp))
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(HerdrSessionStartError) as ctx:
                    prepare_session(
                        repo_root=wt,
                        providers=["codex"],
                        lane_id="issue_13377_y",
                        env={HERDR_ENV: str(binpath)},
                        runner=herdr.run,
                    )
        self.assertIn("main checkout has no registered workspace identity", str(ctx.exception))

    def test_prepare_session_linked_worktree_without_lane_fails_closed(self) -> None:
        """No --lane and no lane metadata record: refuse to mint the project's
        DEFAULT slots (the coordinator pair) from a lane checkout."""
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13377_z")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = self._binpath(Path(tmp))
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                with self.assertRaises(HerdrSessionStartError) as ctx:
                    prepare_session(
                        repo_root=wt,
                        providers=["codex", "claude"],
                        lane_id="",
                        env={HERDR_ENV: str(binpath)},
                        runner=herdr.run,
                    )
        self.assertIn("requires an explicit lane id", str(ctx.exception))

    def test_prepare_session_linked_worktree_recovers_lane_from_metadata(self) -> None:
        """A relaunch without --lane recovers the recorded lane id (never default)."""
        from mozyo_bridge.core.state.lane_metadata import record_lane_created
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            self._init_repo(main)
            wt = Path(tmp) / "lane"
            self._git(main, "worktree", "add", str(wt), "-b", "issue_13377_w")
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = self._binpath(Path(tmp))
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(main)
                main_ws = read_anchor(main)["workspace_id"]
                record_lane_created(
                    lane_workspace_token=derive_lane_workspace_token(str(wt.resolve())),
                    repo_workspace_id=main_ws,
                    lane_label="issue_13377_w",
                    lane_id="issue_13377_w",
                    worktree_path=str(wt),
                )
                herdr = _Herdr()
                result = prepare_session(
                    repo_root=wt,
                    providers=["codex"],
                    lane_id="",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(result.lane_id, "issue_13377_w")
        names = {s.provider: s.assigned_name for s in result.slots}
        self.assertEqual(
            names["codex"], encode_assigned_name(main_ws, "codex", "issue_13377_w")
        )


class LaneTabSubdivisionTest(unittest.TestCase):
    """Lane=tab / gateway+worker=split placement (Redmine #13411).

    A non-default lane's gateway + worker land in ONE dedicated herdr tab inside
    the sublane host workspace; the default lane never uses a tab. Argv-level pins
    drive the local ``_Herdr``; a full-topology pin drives the shared ``FakeHerdr``
    (real tab lifecycle: create → split → reclaim → auto-vanish on retire).
    """

    def _prepare(self, tmp, *, herdr, providers, lane, existing_rows=None):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            result = prepare_session(
                repo_root=repo,
                providers=providers,
                lane_id=lane,
                env={HERDR_ENV: str(binpath)},
                runner=herdr.run,
            )
            ws = read_anchor(repo)["workspace_id"]
        return result, ws

    def test_fresh_lane_creates_dedicated_tab_and_splits_pair(self) -> None:
        # A fresh non-default lane mints ONE tab (labelled with the lane key), lands
        # the first slot in it (no --split), the second beside it (--split right),
        # and reclaims the host base pane AND the tab root pane.
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
            )
        self.assertEqual(len(herdr.tab_creates), 1)
        tab_create = herdr.tab_creates[0]
        self.assertEqual(tab_create[tab_create.index("--workspace") + 1], "wZ")
        self.assertEqual(tab_create[tab_create.index("--label") + 1], "lane-1")
        # First launch (codex) occupies the tab with no split; the second (claude)
        # splits right beside it. Both carry `--tab wZ:t1`.
        codex_argv = herdr.start_argvs[0]
        claude_argv = herdr.start_argvs[1]
        self.assertEqual(codex_argv[codex_argv.index("--tab") + 1], "wZ:t1")
        self.assertNotIn("--split", codex_argv)
        self.assertEqual(claude_argv[claude_argv.index("--tab") + 1], "wZ:t1")
        self.assertEqual(claude_argv[claude_argv.index("--split") + 1], "right")
        # `--tab` sits right after `--workspace` (before the `-- provider` tail).
        self.assertEqual(
            codex_argv[codex_argv.index("--workspace") : codex_argv.index("--workspace") + 4],
            ["--workspace", "wZ", "--tab", "wZ:t1"],
        )
        self.assertEqual(result.herdr_tab_id, "wZ:t1")
        self.assertEqual(result.tab_pane_id, "wZ:t1-root")
        self.assertTrue(result.tab_pane_reclaimed)
        self.assertEqual(
            herdr.pane_closes,
            [["pane", "close", "wZ:p1"], ["pane", "close", "wZ:t1-root"]],
        )

    def test_default_lane_uses_no_tab_byte_invariant(self) -> None:
        # The coordinator pair (default lane) never subdivides: no tab create, no
        # `--tab` / `--split` in any launch argv, no tab fields set.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane=""
            )
        self.assertEqual(herdr.tab_creates, [])
        for argv in herdr.start_argvs:
            self.assertNotIn("--tab", argv)
            self.assertNotIn("--split", argv)
        self.assertEqual(result.herdr_tab_id, "")
        self.assertEqual(result.tab_pane_id, "")
        self.assertEqual(herdr.pane_closes, [["pane", "close", "wZ:p1"]])

    def test_heal_rejoins_the_same_tab_and_splits(self) -> None:
        # A heal (one slot already live in a tab) rejoins the SAME tab and splits
        # the relaunched slot beside its sibling — never a fresh tab, never a base
        # pane, never a tab root reclaim.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                from mozyo_bridge.core.state.workspace_registry import register_workspace

                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                herdr = _Herdr(
                    existing_rows=[
                        {
                            "name": encode_assigned_name(ws, "codex", "lane-1"),
                            "pane_id": "w5:pC",
                            "tab_id": "w5:t3",
                        }
                    ]
                )
                result = prepare_session(
                    repo_root=repo,
                    providers=["codex", "claude"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        # codex adopts; claude launches into the adopted tab with --split right.
        self.assertEqual(herdr.tab_creates, [])
        self.assertEqual(len(herdr.start_argvs), 1)
        claude_argv = herdr.start_argvs[0]
        self.assertEqual(claude_argv[claude_argv.index("--workspace") + 1], "w5")
        self.assertEqual(claude_argv[claude_argv.index("--tab") + 1], "w5:t3")
        self.assertEqual(claude_argv[claude_argv.index("--split") + 1], "right")
        self.assertEqual(result.herdr_tab_id, "w5:t3")
        self.assertEqual(result.tab_pane_id, "")
        self.assertEqual(herdr.pane_closes, [])

    def test_legacy_loose_lane_heal_stays_loose(self) -> None:
        # A heal of a pre-#13411 lane whose live slot is a LOOSE pane (no tab_id)
        # launches loose too — keeping the pair together — never minting a fresh tab
        # that would split them (it migrates to a tab on a full relaunch).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                from mozyo_bridge.core.state.workspace_registry import register_workspace

                register_workspace(repo, home=home)
                ws = read_anchor(repo)["workspace_id"]
                herdr = _Herdr(
                    existing_rows=[
                        {
                            "name": encode_assigned_name(ws, "codex", "lane-1"),
                            "pane_id": "w5:pC",  # no tab_id -> loose pre-#13411 slot
                        }
                    ]
                )
                result = prepare_session(
                    repo_root=repo,
                    providers=["codex", "claude"],
                    lane_id="lane-1",
                    env={HERDR_ENV: str(binpath)},
                    runner=herdr.run,
                )
        self.assertEqual(herdr.tab_creates, [])
        claude_argv = herdr.start_argvs[0]
        self.assertEqual(claude_argv[claude_argv.index("--workspace") + 1], "w5")
        self.assertNotIn("--tab", claude_argv)  # loose, matching the live sibling
        self.assertEqual(result.herdr_tab_id, "")
        self.assertEqual(herdr.pane_closes, [])

    def test_malformed_tab_create_fails_closed_before_launch(self) -> None:
        # An unparseable `tab create` payload fails closed BEFORE any launch — the
        # host base pane is residue (never reclaimed), no agent is started.
        herdr = _Herdr(created_workspace="wZ", tab_bad_payload=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError) as ctx:
                self._prepare(
                    tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
                )
        self.assertIn("tab create", str(ctx.exception))
        self.assertEqual(len(herdr.workspace_creates), 1)
        self.assertEqual(len(herdr.tab_creates), 1)
        self.assertFalse([c for c in herdr.calls if c[:2] == ["agent", "start"]])
        self.assertEqual(herdr.pane_closes, [])  # nothing reclaimed (raised first)

    def test_tab_root_reclaim_failure_is_non_fatal(self) -> None:
        # A tab root `pane close` failure is cosmetic residue only — the agents are
        # live — so it is recorded, not raised (the tab analogue of #13330 j#73225).
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1", close_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
            )
        self.assertFalse(result.tab_pane_reclaimed)
        self.assertTrue(result.tab_pane_detail)
        self.assertTrue(all(s.outcome == SLOT_LAUNCHED for s in result.slots))

    def test_multi_lane_shares_one_host_with_a_tab_each(self) -> None:
        # End-to-end through the shared FakeHerdr (real tab lifecycle): two lanes
        # land in ONE host workspace, each in its OWN tab with a split pair — the
        # 7-lane = 7-tab density reduction, proven at N=2.
        fake = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            env = {HERDR_ENV: str(binpath)}
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                a = prepare_session(
                    repo_root=repo, providers=["codex", "claude"],
                    lane_id="lane-a", env=env, runner=fake.run,
                )
                b = prepare_session(
                    repo_root=repo, providers=["codex", "claude"],
                    lane_id="lane-b", env=env, runner=fake.run,
                )
        # Both lanes share the single host workspace.
        self.assertEqual(a.herdr_workspace_id, b.herdr_workspace_id)
        host = a.herdr_workspace_id
        self.assertEqual(fake.workspace_ids, [host])
        # Two distinct tabs, one per lane, each with exactly its split pair.
        self.assertEqual(a.herdr_tab_id, fake.tab_ids(host)[0])
        self.assertEqual(b.herdr_tab_id, fake.tab_ids(host)[1])
        self.assertEqual(len(fake.tab_ids(host)), 2)
        agent_tabs = {ag["name"]: fake.tab_of(ag["pane_id"]) for ag in fake.agents}
        for lane_tab in (a.herdr_tab_id, b.herdr_tab_id):
            in_tab = [name for name, tab in agent_tabs.items() if tab == lane_tab]
            self.assertEqual(len(in_tab), 2)  # gateway + worker split pair
        # The base + tab root panes were reclaimed: only the 4 agent panes remain.
        self.assertEqual(len(fake.panes_of(host)), 4)

    def test_tab_target_for_lane_placement_rules(self) -> None:
        # Pure decision function (Redmine #13411): own tab pins first, multi-tab
        # fails closed, no own slots -> "" (caller mints / stays loose).
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
            _tab_target_for_lane,
        )

        ws = "wsA"

        def row(role, lane, pane, tab=None):
            r = {"name": encode_assigned_name(ws, role, lane), "pane_id": pane}
            if tab is not None:
                r["tab_id"] = tab
            return r

        # 1. own live slot pins its tab within the host workspace.
        rows = [row("codex", "lane-a", "w8:p2", "w8:t1")]
        self.assertEqual(_tab_target_for_lane(rows, ws, "w8", "lane-a"), "w8:t1")
        # 2. a different lane's slots never pin this lane's tab.
        self.assertEqual(_tab_target_for_lane(rows, ws, "w8", "lane-b"), "")
        # 3. own loose slot (no tab_id) pins nothing -> "" (loose heal downstream).
        loose = [row("codex", "lane-a", "w8:p2")]
        self.assertEqual(_tab_target_for_lane(loose, ws, "w8", "lane-a"), "")
        # 4. a slot outside the target workspace never pins the tab.
        self.assertEqual(_tab_target_for_lane(rows, ws, "w9", "lane-a"), "")
        # 5. fail-closed: own slots span two tabs in the host.
        split = [
            row("codex", "lane-a", "w8:p2", "w8:t1"),
            row("claude", "lane-a", "w8:p3", "w8:t2"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            _tab_target_for_lane(split, ws, "w8", "lane-a")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
