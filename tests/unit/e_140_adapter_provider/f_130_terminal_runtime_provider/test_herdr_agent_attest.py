"""Tests for the startup self-attestation self-check wrapper (Redmine #13637).

The wrapper runs as the herdr-spawned agent process, inspects its OWN env, records a
generation-bound self-attestation, then execs the provider. These tests drive
:func:`perform_self_attestation` with an injected ``lister`` + ``env`` + home (no live
herdr), and the CLI entry's argv handling with ``os.execvp`` patched (so the process
is not actually replaced).
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    VERDICT_CONFLICT,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    _live_lister,
    cmd_herdr_agent_attest,
    perform_self_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    MOZYO_PROVIDER_ARGV0_ENV,
)

NAME = "mzb1_ws1_claude_default"
_GOOD_ENV = {
    "MOZYO_WORKSPACE_ID": "ws1",
    "MOZYO_AGENT_ROLE": "claude",
    "MOZYO_LANE_ID": "default",
}


def _lister(*rows):
    return lambda: list(rows)


class PerformSelfAttestationTest(unittest.TestCase):
    def test_matching_env_records_present_with_self_resolved_locator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=home,
            )
            self.assertEqual(rec.verdict, VERDICT_PRESENT)
            self.assertEqual(rec.locator, "wY:p2")
            # persisted and readable
            got = HerdrIdentityAttestationStore(home=home).read(NAME)
            self.assertEqual(got.verdict, VERDICT_PRESENT)
            self.assertEqual(got.locator, "wY:p2")

    def test_envless_boot_records_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env={"MOZYO_HERDR_BINARY": "/x/herdr"},  # triplet absent
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.verdict, VERDICT_MISSING)

    def test_wrong_env_records_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env={
                    "MOZYO_WORKSPACE_ID": "wsOTHER",
                    "MOZYO_AGENT_ROLE": "claude",
                    "MOZYO_LANE_ID": "default",
                },
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.verdict, VERDICT_CONFLICT)

    def test_ambiguous_self_lookup_records_empty_locator(self) -> None:
        # Two rows with this name (or none) -> no unambiguous locator; recorded empty,
        # which the read side treats as stale / fail-closed.
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_lister(
                    {"name": NAME, "pane_id": "wY:p2"},
                    {"name": NAME, "pane_id": "wZ:p9"},
                ),
                home=Path(tmp),
            )
            self.assertEqual(rec.locator, "")

    def test_lister_failure_records_empty_locator_not_raises(self) -> None:
        def _boom():
            raise RuntimeError("herdr down")

        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                lister=_boom,
                home=Path(tmp),
            )
            self.assertEqual(rec.locator, "")
            self.assertEqual(rec.verdict, VERDICT_PRESENT)


    def test_replacement_action_id_is_recorded(self) -> None:
        # Redmine #13806 tranche D R2-F2: a replacement launch's action id reaches the record.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                replacement_action_id="recover:l:worker:claude:wk:w2",
                lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=home,
            )
            self.assertEqual(rec.replacement_action_id, "recover:l:worker:claude:wk:w2")

    def test_normal_launch_records_empty_action_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME, workspace_id="ws1", role="claude", lane="default",
                env=_GOOD_ENV, lister=_lister({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.replacement_action_id, "")


class LiveListerTerminalIsolationTest(unittest.TestCase):
    """The pre-exec self-attestation lister must never touch the pane terminal (#14017).

    The wrapper runs inside the herdr-spawned pane and is about to ``execvp`` the
    interactive provider into that same pane. Its ``herdr agent list`` child is kept
    off the pane's controlling terminal on every fd, so it can never perturb the
    stdin / foreground state the provider inherits — the disturbance that made Claude
    exit into ``shell_residue`` while Codex survived.
    """

    _ENV = {"MOZYO_HERDR_BINARY": "/x/herdr", "PATH": "/usr/bin"}

    def _run_lister(self, run_mock):
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.subprocess.run",
            run_mock,
        ):
            return _live_lister(self._ENV)()

    def test_list_subprocess_detaches_from_pane_terminal(self) -> None:
        import subprocess as _sp

        def _run(argv, **kwargs):
            self.assertEqual(argv, ["/x/herdr", "agent", "list"])
            # Off the pane terminal on every standard fd: stdout/stderr piped,
            # stdin from /dev/null, and its own session (no controlling terminal).
            self.assertEqual(kwargs.get("stdin"), _sp.DEVNULL)
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertTrue(kwargs.get("capture_output"))
            return argparse.Namespace(
                returncode=0, stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}]'
            )

        rows = self._run_lister(_run)
        # The isolation kwargs do not change functionality: a valid payload still parses.
        self.assertEqual(rows, [{"name": NAME, "pane_id": "wY:p2"}])


class CmdAgentAttestTest(unittest.TestCase):
    def _args(self, provider_argv, replacement_action_id=""):
        return argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id=replacement_action_id,
            provider_argv=provider_argv,
        )

    def test_replacement_action_id_flag_reaches_the_record(self) -> None:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"MOZYO_BRIDGE_HOME": tmp, "MOZYO_HERDR_BINARY": "/x/herdr",
             "MOZYO_WORKSPACE_ID": "ws1", "MOZYO_AGENT_ROLE": "claude", "MOZYO_LANE_ID": "default"},
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest._live_lister",
            return_value=_lister({"name": NAME, "pane_id": "wY:p2"}),
        ), patch("os.execvp") as execvp:
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(
                    self._args(["--", "claude"], replacement_action_id="recover:xyz")
                )
            back = HerdrIdentityAttestationStore(home=Path(tmp)).read(NAME)
            self.assertIsNotNone(back)
            self.assertEqual(back.replacement_action_id, "recover:xyz")

    def test_execs_provider_after_stripping_separator(self) -> None:
        # The CLI records then execs the provider argv, dropping the argparse
        # REMAINDER leading `--`.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": tmp, "MOZYO_HERDR_BINARY": "/x/herdr"}
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest._live_lister",
            return_value=_lister({"name": NAME, "pane_id": "wY:p2"}),
        ), patch(
            "os.execvp"
        ) as execvp:
            # Simulate exec replacing the process (so the unreachable guard is not hit).
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(
                    self._args(["--", "claude", "--permission-mode", "auto"])
                )
            execvp.assert_called_once_with(
                "claude", ["claude", "--permission-mode", "auto"]
            )

    def test_missing_provider_argv_fails_closed(self) -> None:
        with patch("os.execvp") as execvp:
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(self._args([]))
            execvp.assert_not_called()


class CmdAgentAttestArgv0DecouplingTest(unittest.TestCase):
    """Redmine #14017: exec the realpath, present the trusted alias as argv[0].

    The exec target is always ``provider_argv[0]`` (the verified realpath); the alias,
    when the launch injected ``MOZYO_PROVIDER_ARGV0``, is applied as argv[0] DATA only —
    via ``os.execv`` (explicit path, no PATH search) so the alias itself is never run.
    """

    def _args(self, provider_argv):
        return argparse.Namespace(
            assigned_name=NAME,
            workspace_id="ws1",
            role="claude",
            lane="default",
            replacement_action_id="",
            provider_argv=provider_argv,
        )

    def _run(self, provider_argv, env):
        base = {
            "MOZYO_BRIDGE_HOME": "/tmp/x",
            "MOZYO_HERDR_BINARY": "/x/herdr",
        }
        base.update(env)
        with patch.dict("os.environ", base, clear=True), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest._live_lister",
            return_value=_lister({"name": NAME, "pane_id": "wY:p2"}),
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.record_identity_attestation",
            return_value=None,
        ), patch("os.execv") as execv, patch("os.execvp") as execvp:
            execv.side_effect = SystemExit(0)
            execvp.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cmd_herdr_agent_attest(self._args(provider_argv))
            leftover = os.environ.get(MOZYO_PROVIDER_ARGV0_ENV)
        return execv, execvp, leftover

    def test_absolute_alias_env_execs_realpath_with_alias_argv0(self) -> None:
        execv, execvp, leftover = self._run(
            ["--", "/opt/claude/cli", "--permission-mode", "auto"],
            {MOZYO_PROVIDER_ARGV0_ENV: "/home/u/.local/bin/claude"},
        )
        execv.assert_called_once_with(
            "/opt/claude/cli", ["/home/u/.local/bin/claude", "--permission-mode", "auto"]
        )
        execvp.assert_not_called()
        self.assertIsNone(leftover)  # dropped from the provider's inherited env

    def test_no_alias_env_execvp_byte_invariant(self) -> None:
        execv, execvp, _ = self._run(
            ["--", "/opt/codex", "-c", "x=1"], {}
        )
        execvp.assert_called_once_with("/opt/codex", ["/opt/codex", "-c", "x=1"])
        execv.assert_not_called()

    def test_relative_alias_env_is_ignored(self) -> None:
        # A non-absolute alias must never become argv[0]; fall back to execvp(realpath).
        execv, execvp, _ = self._run(
            ["--", "/opt/claude/cli"], {MOZYO_PROVIDER_ARGV0_ENV: "claude"}
        )
        execvp.assert_called_once_with("/opt/claude/cli", ["/opt/claude/cli"])
        execv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
