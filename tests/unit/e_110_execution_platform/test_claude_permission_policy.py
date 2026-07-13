"""Reproducible Claude auto permission-mode launch policy (Redmine #11925).

Covers four surfaces of the policy:

- the pure resolver / precedence (``env override > launch-context policy
  default > none``) in ``domain.claude_permission_policy``;
- the launch chokepoint ``_agent_launch_command`` honoring the policy
  default while keeping the standalone path's historical bare ``claude``;
- the cockpit ``--dry-run`` reproducibly planning ``claude --permission-mode
  auto`` with the env var unset (the detection surface operators rely on);
- the ``doctor`` ``claude_launch_policy`` diagnostic that flags future
  cockpit / sublane Claude panes that would not launch in auto mode.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
_TESTS_ROOT = Path(__file__).resolve().parents[2]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from support.agent_provider_binaries import FakeAgentBinaries, neutralized_overrides
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    CLAUDE_PERMISSION_MODE_ENV,
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
    InvalidPermissionMode,
    SOURCE_ENV_INVALID,
    SOURCE_ENV_OVERRIDE,
    SOURCE_NONE,
    SOURCE_POLICY_DEFAULT,
    describe_launch_policy,
    permission_mode_flag,
    resolve_claude_permission_mode,
)

# Since #13441 the tmux launch chokepoint renders argv[0] as the provider's verified
# absolute executable, resolved from the trusted env. These tests clear `os.environ`, so
# they must supply a PATH — a hermetic one, never the host's, so the suite does not
# depend on a real `claude` / `codex` being installed.
PROVIDER_BINS = FakeAgentBinaries(Path(tempfile.mkdtemp(prefix="mzb-perm-bins-")))
atexit.register(shutil.rmtree, PROVIDER_BINS.bin_dir.parent, True)


def _trusted_env(env=None):
    """``env`` with the hermetic provider PATH the launch chokepoint resolves against.

    PATH is applied LAST so it always wins: a scenario that copies the real
    ``os.environ`` would otherwise carry the host's PATH and resolve the developer's
    real ``claude`` / ``codex``, which is exactly what these tests must not do.
    """
    return {
        **(env or {}),
        "PATH": str(PROVIDER_BINS.bin_dir),
        # Blank any trusted override inherited from the developer's environment: an
        # override BEATS PATH, so without this the fixture PATH is not authoritative.
        **neutralized_overrides(),
    }


def _argv0(provider):
    """The shell-quoted absolute argv[0] the launch command must render for ``provider``."""
    return shlex.quote(PROVIDER_BINS.path(provider))


class ResolverPrecedenceTest(unittest.TestCase):
    """`env override > launch-context policy default > none`, Claude-only."""

    def test_cockpit_default_yields_auto_when_env_unset(self) -> None:
        mode = resolve_claude_permission_mode(
            "claude", policy_default="auto", env={}
        )
        self.assertEqual("auto", mode)

    def test_env_override_wins_over_policy_default(self) -> None:
        # The env var is the compatibility / explicit override rail: an
        # operator can force any mode, including turning auto *off*.
        mode = resolve_claude_permission_mode(
            "claude",
            policy_default="auto",
            env={CLAUDE_PERMISSION_MODE_ENV: "default"},
        )
        self.assertEqual("default", mode)

    def test_no_default_and_no_env_keeps_bare_claude(self) -> None:
        # The standalone `mozyo` window path: historical behavior preserved.
        self.assertIsNone(
            resolve_claude_permission_mode("claude", policy_default=None, env={})
        )

    def test_blank_env_is_treated_as_unset(self) -> None:
        self.assertEqual(
            "auto",
            resolve_claude_permission_mode(
                "claude",
                policy_default="auto",
                env={CLAUDE_PERMISSION_MODE_ENV: "   "},
            ),
        )

    def test_codex_never_gets_a_mode(self) -> None:
        self.assertIsNone(
            resolve_claude_permission_mode(
                "codex",
                policy_default="auto",
                env={CLAUDE_PERMISSION_MODE_ENV: "auto"},
            )
        )

    def test_invalid_env_value_raises(self) -> None:
        with self.assertRaises(InvalidPermissionMode):
            resolve_claude_permission_mode(
                "claude", env={CLAUDE_PERMISSION_MODE_ENV: "autopilot"}
            )

    def test_invalid_policy_default_raises(self) -> None:
        with self.assertRaises(InvalidPermissionMode):
            resolve_claude_permission_mode(
                "claude", policy_default="autopilot", env={}
            )

    def test_flag_rendering(self) -> None:
        self.assertEqual(
            " --permission-mode auto",
            permission_mode_flag("claude", policy_default="auto", env={}),
        )
        self.assertEqual("", permission_mode_flag("codex", policy_default="auto", env={}))
        self.assertEqual("", permission_mode_flag("claude", policy_default=None, env={}))


class DescribeLaunchPolicyTest(unittest.TestCase):
    def test_default_describes_reproducible_auto(self) -> None:
        policy = describe_launch_policy(env={})
        self.assertEqual("auto", policy["effective_mode"])
        self.assertEqual(SOURCE_POLICY_DEFAULT, policy["source"])
        self.assertTrue(policy["reproducible_auto"])

    def test_env_override_is_labeled(self) -> None:
        policy = describe_launch_policy(env={CLAUDE_PERMISSION_MODE_ENV: "auto"})
        self.assertEqual(SOURCE_ENV_OVERRIDE, policy["source"])
        self.assertTrue(policy["reproducible_auto"])

    def test_env_override_turning_auto_off_is_not_reproducible_auto(self) -> None:
        policy = describe_launch_policy(env={CLAUDE_PERMISSION_MODE_ENV: "default"})
        self.assertEqual(SOURCE_ENV_OVERRIDE, policy["source"])
        self.assertEqual("default", policy["effective_mode"])
        self.assertFalse(policy["reproducible_auto"])

    def test_invalid_env_is_reported_not_raised(self) -> None:
        policy = describe_launch_policy(env={CLAUDE_PERMISSION_MODE_ENV: "nope"})
        self.assertEqual(SOURCE_ENV_INVALID, policy["source"])
        self.assertIsNone(policy["effective_mode"])
        self.assertFalse(policy["env_valid"])

    def test_no_policy_default_is_none_source(self) -> None:
        policy = describe_launch_policy(policy_default=None, env={})
        self.assertEqual(SOURCE_NONE, policy["source"])
        self.assertFalse(policy["reproducible_auto"])


class LaunchChokepointTest(unittest.TestCase):
    """`_agent_launch_command` honors the policy default and the override."""

    def _command(self, agent, *, policy_default, env):
        from mozyo_bridge.application.commands import _agent_launch_command

        with patch.dict("os.environ", _trusted_env(env), clear=True):
            return _agent_launch_command(
                agent, "mozyo-demo", cwd=None, permission_mode_default=policy_default
            )

    def test_cockpit_default_renders_auto_without_env(self) -> None:
        cmd = self._command("claude", policy_default="auto", env={})
        self.assertTrue(cmd.endswith(f" {_argv0('claude')} --permission-mode auto"), cmd)

    def test_standalone_default_keeps_bare_claude(self) -> None:
        cmd = self._command("claude", policy_default=None, env={})
        self.assertTrue(cmd.endswith(f" {_argv0('claude')}"), cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_env_override_beats_cockpit_default(self) -> None:
        cmd = self._command(
            "claude",
            policy_default="auto",
            env={CLAUDE_PERMISSION_MODE_ENV: "plan"},
        )
        self.assertTrue(cmd.endswith(f" {_argv0('claude')} --permission-mode plan"), cmd)

    def test_codex_unaffected_by_cockpit_default(self) -> None:
        cmd = self._command("codex", policy_default="auto", env={})
        self.assertTrue(cmd.endswith(f" {_argv0('codex')}"), cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_invalid_env_is_hard_error(self) -> None:
        with self.assertRaises(SystemExit):
            self._command(
                "claude",
                policy_default="auto",
                env={CLAUDE_PERMISSION_MODE_ENV: "autopilot"},
            )


class LaunchChokepointModelFlagTest(unittest.TestCase):
    """`_agent_launch_command` renders the #13155 `--model` flag after the mode."""

    def _command(self, agent, *, policy_default=None, claude_model=None, env=None):
        from mozyo_bridge.application.commands import _agent_launch_command

        with patch.dict("os.environ", _trusted_env(env), clear=True):
            return _agent_launch_command(
                agent,
                "mozyo-demo",
                cwd=None,
                permission_mode_default=policy_default,
                claude_model=claude_model,
            )

    def test_model_flag_renders_after_permission_mode(self) -> None:
        cmd = self._command(
            "claude", policy_default="auto", claude_model="claude-opus-4-8"
        )
        self.assertTrue(
            cmd.endswith(f" {_argv0('claude')} --permission-mode auto --model claude-opus-4-8"),
            cmd,
        )

    def test_model_flag_without_permission_mode(self) -> None:
        cmd = self._command("claude", policy_default=None, claude_model="sonnet")
        self.assertTrue(cmd.endswith(f" {_argv0('claude')} --model sonnet"), cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_no_model_is_byte_identical_to_historical(self) -> None:
        # Characterization: an unset model leaves the launch command exactly as
        # it was before #13155 — no `--model` fragment anywhere.
        with_model_unset = self._command("claude", policy_default="auto")
        historical = self._command("claude", policy_default="auto", claude_model=None)
        self.assertEqual(with_model_unset, historical)
        self.assertTrue(
            historical.endswith(f" {_argv0('claude')} --permission-mode auto"), historical
        )
        self.assertNotIn("--model", historical)

    def test_codex_never_gets_model_flag(self) -> None:
        cmd = self._command("codex", claude_model="claude-opus-4-8")
        self.assertTrue(cmd.endswith(f" {_argv0('codex')}"), cmd)
        self.assertNotIn("--model", cmd)

    def test_invalid_model_is_hard_error(self) -> None:
        with self.assertRaises(SystemExit):
            self._command("claude", policy_default="auto", claude_model="bad; rm -rf")


class CockpitDryRunPolicyTest(unittest.TestCase):
    """End-to-end: `mozyo layout --dry-run` plans reproducible auto Claude.

    Uses the real `_agent_launch_command` (not a stub) with the env var
    unset, proving the cockpit launch path itself supplies the auto policy
    default — the dry-run surface the acceptance criteria require for
    detecting that future Claude panes will be auto.
    """

    def _args(self, **over):
        base = dict(
            preset="cockpit",
            codex_ratio=70,
            cockpit_session=None,
            layout_repos=["/repoA"],
            dry_run=True,
            json_output=False,
            cc=False,
            no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_dry_run_plans_claude_auto_and_leaves_codex_bare(self) -> None:
        from mozyo_bridge.application import commands

        def fake_resolve(repo, **_k):
            name = "sess-" + Path(repo).name
            return argparse.Namespace(name=name, workspace_id="id-" + name)

        env = {k: v for k, v in __import__("os").environ.items()}
        env.pop(CLAUDE_PERMISSION_MODE_ENV, None)
        with patch.object(commands, "resolve_canonical_session", side_effect=fake_resolve), \
            patch.dict("os.environ", _trusted_env(env), clear=True), \
            patch.object(commands, "run_tmux") as run_tmux:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = commands.cmd_layout_apply(self._args())
        self.assertEqual(0, rc)
        run_tmux.assert_not_called()
        text = out.getvalue()
        # Claude pane reproducibly auto; codex pane untouched.
        self.assertIn("claude --permission-mode auto", text)
        codex_lines = [
            ln for ln in text.splitlines() if " codex" in ln and "--permission-mode" in ln
        ]
        self.assertEqual([], codex_lines, text)


class DoctorLaunchPolicySectionTest(unittest.TestCase):
    def _section(self, env):
        from mozyo_bridge.application.doctor import (
            doctor_claude_launch_policy_section,
        )

        with patch.dict("os.environ", _trusted_env(env), clear=True):
            return doctor_claude_launch_policy_section()

    def test_unset_env_reports_ok_reproducible_auto(self) -> None:
        section = self._section({})
        self.assertEqual("ok", section["status"])
        self.assertEqual("auto", section["effective_mode"])
        self.assertTrue(section["reproducible_auto"])
        self.assertIn("non-retroactive", section["scope"])

    def test_env_override_to_auto_is_ok(self) -> None:
        section = self._section({CLAUDE_PERMISSION_MODE_ENV: "auto"})
        self.assertEqual("ok", section["status"])

    def test_env_override_off_is_warning(self) -> None:
        section = self._section({CLAUDE_PERMISSION_MODE_ENV: "default"})
        self.assertEqual("warning", section["status"])
        self.assertTrue(section["next_action"])

    def test_invalid_env_is_warning(self) -> None:
        section = self._section({CLAUDE_PERMISSION_MODE_ENV: "autopilot"})
        self.assertEqual("warning", section["status"])
        self.assertTrue(section["next_action"])

    def test_section_flips_overall_doctor_when_override_off(self) -> None:
        from mozyo_bridge.application.doctor import run_doctor

        args = argparse.Namespace(repo=str(ROOT), home=None)
        # The target is this checkout, whose repo-local config may select the
        # herdr backend: stub the #13355 herdr section so this test never
        # performs a live `herdr agent list` read (hermeticity, #13359 lesson).
        with patch.dict(
            "os.environ", {CLAUDE_PERMISSION_MODE_ENV: "default"}, clear=False
        ), patch(
            "mozyo_bridge.application.doctor.doctor_herdr_section",
            return_value=None,
        ):
            result = run_doctor(args)
        self.assertIn("claude_launch_policy", result["sections"])
        self.assertEqual(
            "warning", result["sections"]["claude_launch_policy"]["status"]
        )
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
