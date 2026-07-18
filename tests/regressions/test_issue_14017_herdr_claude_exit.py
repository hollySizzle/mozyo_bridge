"""Regression pins for Redmine #14017 — Herdr Claude provider exit into shell_residue.

**Root cause (R3, j#81879).** Under Herdr, mozyo's trusted executable resolver collapsed
Claude's stable alias to its symlink-resolved realpath and used that realpath as the
provider's ``argv[0]`` too (Design Answer j#76725 Q1). Claude's interactive TUI, exec'd
with the realpath as ``argv[0]``, exits immediately into ``shell_residue``; invoked as
its trusted absolute alias (``~/.local/bin/claude``) with the SAME realpath as the exec
target, it stays resident. The bounded probe matrix isolated this exactly: realpath
argv[0] exits (direct install, temp wrapper, ``auto`` and ``plan`` modes alike); alias
argv[0] with a realpath exec target stays resident; and a normal PTY tolerates realpath
argv[0]. ``_verify_executable_realpath`` intentionally pins the exec target to the
realpath (against a PATH / symlink TOCTOU), so the alias must NOT be executed directly.

**Fix.** The resolver now yields a distinct ``exec_target`` (verified realpath — what
runs) and ``argv0`` (the trusted absolute alias — argv[0] data only). A wrapped managed
launch of a provider whose alias differs from its realpath injects ``--env
MOZYO_PROVIDER_ARGV0=<alias>``; the ``herdr agent-attest`` wrapper reads it and
``os.execv``s the realpath while handing the process ``argv[0]=<alias>``. The exec target
is always the realpath (trust boundary preserved); only the invocation identity is the
alias. Without the var — an unsymlinked provider, an unwrapped launch, or an older
wrapper — the provider is ``os.execvp``'d at the realpath on both argv[0] and exec
target: the honest fallback that never weakens trust by execing an alias, byte-invariant
with the pre-#14017 form.

**R1 correction.** R1 (commit 86fc24bc) blamed the wrapper's pre-exec ``herdr agent
list`` self-lookup (``_live_lister``) for perturbing the pane terminal and shipped a
subprocess detach as the fix. Installed dogfood refuted it: Claude still exited with the
lister detached (j#81858) and with the whole wrapper removed (j#81867). The detach is
retained here purely as harmless terminal hygiene, NOT as the #14017 fix, and this file's
narrative no longer claims the lister was the root cause.

These pins are deterministic (no live herdr / no real provider). Fix lives in
``.../f_160_provider_registry/application/agent_provider_executable.py``,
``.../f_130_terminal_runtime_provider/application/herdr_launch_argv.py``, and
``.../f_130_terminal_runtime_provider/application/herdr_agent_attest.py``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    cmd_herdr_agent_attest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    MOZYO_PROVIDER_ARGV0_ENV,
    build_agent_start_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    AgentProviderExecutableError,
    ResolvedProviderLaunch,
    resolve_agent_launch,
)

NAME = "mzb1_ws1_claude_default"
_MODULE = (
    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
    "application.herdr_agent_attest"
)


def _install_exe(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return os.path.realpath(str(target))


def _symlinked_alias(root: Path, name: str) -> tuple[str, str]:
    """A ``<root>/bin/<name>`` symlink to a real exe under ``<root>/real``.

    Mirrors how Claude ships: a stable alias on PATH pointing at a versioned install.
    Returns ``(alias_path, exec_realpath)`` with ``alias_path != exec_realpath``.
    """
    real = _install_exe(root / "real", f"{name}-real")
    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    alias = bindir / name
    os.symlink(real, alias)
    return str(alias), real


def _env_pairs(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, tok in enumerate(argv):
        if tok == "--env" and i + 1 < len(argv) and "=" in argv[i + 1]:
            key, _, value = argv[i + 1].partition("=")
            out[key] = value
    return out


def _build(resolved: ResolvedProviderLaunch, *, attest_launcher: str) -> list[str]:
    return build_agent_start_argv(
        assigned_name=NAME,
        provider=resolved.provider_id,
        repo_root=Path("/repo"),
        workspace_id="ws1",
        lane="default",
        target_workspace="wsX",
        target_tab="",
        split="",
        focus=False,
        binary="/x/herdr",
        attest_launcher=attest_launcher,
        store_home="/home/store",
        resolved=resolved,
        launch_argv_extra=[],
    )


class Argv0DecouplingResolverTest(unittest.TestCase):
    """The resolver separates the exec-target realpath from the trusted argv[0] alias."""

    def _blank_overrides(self) -> dict[str, str]:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
            AGENT_PROVIDER_PROFILES,
        )

        return {p.executable.env_override: "" for p in AGENT_PROVIDER_PROFILES}

    def test_symlinked_claude_alias_is_argv0_realpath_is_exec_target(self) -> None:
        # Claude on PATH via its stable symlink: exec target = realpath (what runs),
        # argv0 = the trusted alias (argv[0] data). This is the decoupling #14017 needs.
        with tempfile.TemporaryDirectory() as tmp:
            # Canonicalize the base so only the deliberate leaf symlink distinguishes the
            # alias from the realpath (macOS lands mkdtemp behind a /var -> /private/var
            # symlink).
            base = Path(os.path.realpath(tmp))
            alias, real = _symlinked_alias(base, "claude")
            env = {"PATH": str(base / "bin"), **self._blank_overrides()}
            resolved = resolve_agent_launch("claude", env)
            # The alias resolves TO the exec target — it is never itself executed.
            self.assertEqual(os.path.realpath(resolved.argv0), resolved.exec_target)
        self.assertEqual(resolved.exec_target, real)
        self.assertEqual(resolved.argv0, alias)
        self.assertNotEqual(resolved.argv0, resolved.exec_target)

    def test_unsymlinked_codex_has_equal_exec_target_and_argv0(self) -> None:
        # A provider installed as a plain file (no symlink) has alias == realpath, so the
        # launch stays byte-invariant with the pre-#14017 single-realpath form.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            real = _install_exe(base / "bin", "codex")
            env = {"PATH": str(base / "bin"), **self._blank_overrides()}
            resolved = resolve_agent_launch("codex", env)
        self.assertEqual(resolved.exec_target, real)
        self.assertEqual(resolved.argv0, real)
        self.assertEqual(resolved.argv0, resolved.exec_target)

    def test_absolute_override_alias_is_argv0_exec_target_is_realpath(self) -> None:
        # An explicit trusted override that points at a symlink: the override value is the
        # argv0 alias, its realpath is the exec target. Executing the override path is
        # never done — only the realpath runs.
        with tempfile.TemporaryDirectory() as tmp:
            alias, real = _symlinked_alias(Path(os.path.realpath(tmp)), "claude")
            env = {
                "PATH": "/nonexistent",
                **self._blank_overrides(),
                "MOZYO_AGENT_CLAUDE_BINARY": alias,
            }
            resolved = resolve_agent_launch("claude", env)
        self.assertEqual(resolved.exec_target, real)
        self.assertEqual(resolved.argv0, alias)

    def test_ambiguous_path_still_fails_closed(self) -> None:
        # Two DISTINCT realpaths on PATH remain an ambiguity fail-closed; the alias split
        # does not soften the trust check.
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            _install_exe(a, "claude")
            _install_exe(b, "claude")
            env = {"PATH": f"{a}{os.pathsep}{b}", **self._blank_overrides()}
            with self.assertRaises(AgentProviderExecutableError) as ctx:
                resolve_agent_launch("claude", env)
        self.assertIn("ambiguously", str(ctx.exception))


class Argv0LaunchArgvTest(unittest.TestCase):
    """``build_agent_start_argv`` carries the alias out-of-band, only when it helps."""

    def test_wrapped_symlinked_provider_injects_argv0_env(self) -> None:
        resolved = ResolvedProviderLaunch(
            provider_id="claude",
            executable="/opt/claude/2.1.214/cli",
            argv0="/home/u/.local/bin/claude",
            managed_argv=("--permission-mode", "auto"),
        )
        argv = _build(resolved, attest_launcher="/abs/mozyo-bridge")
        env = _env_pairs(argv)
        self.assertEqual(env.get(MOZYO_PROVIDER_ARGV0_ENV), "/home/u/.local/bin/claude")
        # The provider command after the LAST `--` (the wrapper's provider separator; the
        # first `--` is herdr's own run-command separator) still starts with the realpath
        # exec target — the alias travels only on --env, never as the exec token.
        dd = len(argv) - 1 - argv[::-1].index("--")
        provider_cmd = argv[dd + 1 :]
        self.assertEqual(provider_cmd[0], "/opt/claude/2.1.214/cli")

    def test_wrapped_unsymlinked_provider_omits_argv0_env(self) -> None:
        # alias == realpath -> no decoupling needed -> byte-invariant (no extra --env).
        resolved = ResolvedProviderLaunch(
            provider_id="codex",
            executable="/opt/codex",
            argv0="/opt/codex",
            tool_shell_env_overrides=True,
        )
        argv = _build(resolved, attest_launcher="/abs/mozyo-bridge")
        self.assertNotIn(MOZYO_PROVIDER_ARGV0_ENV, _env_pairs(argv))

    def test_unwrapped_launch_never_injects_argv0_env(self) -> None:
        # No wrapper to honor the alias -> emit nothing and keep the realpath as argv[0]:
        # the unwrapped fallback cannot decouple, and must not weaken trust by execing an
        # alias, so it stays on the realpath (honest).
        resolved = ResolvedProviderLaunch(
            provider_id="claude",
            executable="/opt/claude/2.1.214/cli",
            argv0="/home/u/.local/bin/claude",
            managed_argv=("--permission-mode", "auto"),
        )
        argv = _build(resolved, attest_launcher="")
        self.assertNotIn(MOZYO_PROVIDER_ARGV0_ENV, _env_pairs(argv))
        dd = argv.index("--")
        # Unwrapped: the run command IS the provider command, argv[0] = realpath.
        self.assertEqual(argv[dd + 1], "/opt/claude/2.1.214/cli")


class Argv0WrapperExecTest(unittest.TestCase):
    """The wrapper execs the realpath but presents the alias as argv[0] (Redmine #14017)."""

    def _drive(self, provider_argv, *, alias, home):
        def _fake_run(argv, **kwargs):
            return argparse.Namespace(
                returncode=0,
                stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}]',
            )

        envd = {
            "MOZYO_BRIDGE_HOME": str(home),
            "MOZYO_HERDR_BINARY": "/x/herdr",
            "MOZYO_WORKSPACE_ID": "ws1",
            "MOZYO_AGENT_ROLE": "claude",
            "MOZYO_LANE_ID": "default",
        }
        if alias:
            envd[MOZYO_PROVIDER_ARGV0_ENV] = alias
        args = argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id="",
            provider_argv=["--", *provider_argv],
        )
        with patch.dict("os.environ", envd, clear=True), patch(
            f"{_MODULE}.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ), patch(f"{_MODULE}.subprocess.run", _fake_run), patch(
            "os.execv"
        ) as execv, patch(
            "os.execvp"
        ) as execvp:
            execv.side_effect = SystemExit(0)
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(args)
            leftover = os.environ.get(MOZYO_PROVIDER_ARGV0_ENV)
        return execv, execvp, leftover

    def test_alias_env_execs_realpath_with_alias_argv0(self) -> None:
        # Claude: exec target = realpath, argv[0] = trusted alias. This is the residency
        # fix — Claude sees its stable alias, not the symlink-collapsed realpath.
        provider_argv = ["/opt/claude/cli", "--permission-mode", "auto"]
        with tempfile.TemporaryDirectory() as tmp:
            execv, execvp, leftover = self._drive(
                provider_argv, alias="/home/u/.local/bin/claude", home=Path(tmp)
            )
        execv.assert_called_once_with(
            "/opt/claude/cli",
            ["/home/u/.local/bin/claude", "--permission-mode", "auto"],
        )
        execvp.assert_not_called()
        # The alias var is a wrapper instruction, dropped from the provider's env.
        self.assertIsNone(leftover)

    def test_no_alias_env_is_byte_invariant_execvp(self) -> None:
        # Codex (and any unsymlinked/older-wrapper launch): no alias var -> execvp the
        # realpath unchanged on both argv[0] and exec target (byte-invariant fallback).
        for label, provider_argv in {
            "codex": ["/opt/codex", "-c", "tool_shell.env.MOZYO_WORKSPACE_ID=ws1"],
            "claude-realpath": ["/opt/claude/cli", "--permission-mode", "auto"],
        }.items():
            with self.subTest(provider=label), tempfile.TemporaryDirectory() as tmp:
                execv, execvp, _ = self._drive(
                    provider_argv, alias="", home=Path(tmp)
                )
                execvp.assert_called_once_with(provider_argv[0], provider_argv)
                execv.assert_not_called()

    def test_relative_alias_env_is_ignored_and_falls_back(self) -> None:
        # Defensive: the resolver only ever emits an ABSOLUTE alias, but a non-absolute
        # value must never become argv[0] — fall back to the byte-invariant execvp.
        provider_argv = ["/opt/claude/cli", "--permission-mode", "auto"]
        with tempfile.TemporaryDirectory() as tmp:
            execv, execvp, leftover = self._drive(
                provider_argv, alias="claude", home=Path(tmp)
            )
        execv.assert_not_called()
        execvp.assert_called_once_with(provider_argv[0], provider_argv)
        self.assertIsNone(leftover)


class PreExecListerHygieneTest(unittest.TestCase):
    """The pre-exec self-lookup stays off the pane terminal (retained R1 hygiene).

    This is NOT the #14017 fix (dogfood refuted the lister root-cause hypothesis, see
    the module docstring); it is kept as sound terminal hygiene for the query subprocess.
    """

    def _args(self, provider_argv):
        return argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id="",
            provider_argv=["--", *provider_argv],
        )

    def test_pre_exec_lister_is_detached_from_the_pane_terminal(self) -> None:
        list_calls: list[dict] = []

        def _fake_run(argv, **kwargs):
            list_calls.append({"argv": list(argv), "kwargs": kwargs})
            return argparse.Namespace(
                returncode=0,
                stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}]',
            )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "MOZYO_BRIDGE_HOME": str(tmp),
                "MOZYO_HERDR_BINARY": "/x/herdr",
                "MOZYO_WORKSPACE_ID": "ws1",
                "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "default",
            },
            clear=True,
        ), patch(
            f"{_MODULE}.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ), patch(f"{_MODULE}.subprocess.run", _fake_run), patch("os.execvp") as execvp:
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(self._args(["/opt/claude/cli"]))
        self.assertTrue(list_calls, "the self-attestation lister must run")
        for call in list_calls:
            self.assertEqual(call["argv"], ["/x/herdr", "agent", "list"])
            kwargs = call["kwargs"]
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertTrue(kwargs.get("capture_output"))


class AttestationRecordedBeforeExecTest(unittest.TestCase):
    """Attestation success precedes a clean exec — on BOTH the alias and fallback paths."""

    def _drive(self, provider_argv, *, alias, home):
        def _fake_run(argv, **kwargs):
            return argparse.Namespace(
                returncode=0,
                stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}]',
            )

        envd = {
            "MOZYO_BRIDGE_HOME": str(home),
            "MOZYO_HERDR_BINARY": "/x/herdr",
            "MOZYO_WORKSPACE_ID": "ws1",
            "MOZYO_AGENT_ROLE": "claude",
            "MOZYO_LANE_ID": "default",
        }
        if alias:
            envd[MOZYO_PROVIDER_ARGV0_ENV] = alias
        args = argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id="",
            provider_argv=["--", *provider_argv],
        )
        with patch.dict("os.environ", envd, clear=True), patch(
            f"{_MODULE}.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ), patch(f"{_MODULE}.subprocess.run", _fake_run), patch(
            "os.execv", side_effect=SystemExit(0)
        ), patch("os.execvp", side_effect=SystemExit(0)):
            try:
                cmd_herdr_agent_attest(args)
            except SystemExit:
                pass

    def test_attestation_recorded_present_on_both_exec_paths(self) -> None:
        for label, alias in {"alias-claude": "/home/u/.local/bin/claude", "fallback": ""}.items():
            with self.subTest(path=label), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                self._drive(["/opt/claude/cli", "--permission-mode", "auto"], alias=alias, home=home)
                record = HerdrIdentityAttestationStore(home=home).read(NAME)
                self.assertIsNotNone(record)
                self.assertEqual(record.verdict, VERDICT_PRESENT)
                self.assertEqual(record.locator, "wY:p2")


if __name__ == "__main__":
    unittest.main()
