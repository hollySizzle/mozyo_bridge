"""Tests for the startup self-attestation self-check wrapper (Redmine #13637).

The wrapper runs as the herdr-spawned agent process, inspects its OWN env, records a
generation-bound self-attestation, then execs the provider. These tests drive
:func:`perform_self_attestation` with an injected ``lister`` + ``env`` + home (no live
herdr), and the CLI entry's argv handling with ``os.execvp`` patched (so the process
is not actually replaced).
"""

from __future__ import annotations

import argparse
import contextlib
import io
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
    SELF_LOOKUP_TOTAL_BUDGET_SECONDS,
    _argv0_alias_binds_to_exec_target,
    bounded_self_lookup,
    cmd_herdr_agent_attest,
    perform_self_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    MOZYO_PROVIDER_ARGV0_ENV,
)

NAME = "mzb1_ws1_claude_default"
# MOZYO_HERDR_BINARY is part of the launcher-injected env (see herdr_launch_argv's
# HERDR_BINARY_ENV): without it the bounded self-lookup stops at `binary_unresolved`
# before it can reach the injected runner at all.
_GOOD_ENV = {
    "MOZYO_WORKSPACE_ID": "ws1",
    "MOZYO_AGENT_ROLE": "claude",
    "MOZYO_LANE_ID": "default",
    "MOZYO_HERDR_BINARY": "/x/herdr",
}


# The CLI's own diagnostics, verbatim (`die` / `warn` in mozyo_bridge.shared.errors emit
# exactly one prefixed line each). Kept as constants so the fail-closed contract has ONE
# spelling that a mutation has to break.
MISSING_PROVIDER_ARGV_ERROR = (
    "error: herdr agent-attest requires a provider command after `--` to exec "
    "(usage: herdr agent-attest --assigned-name ... -- <provider> [args...])"
)
ARGV0_ALIAS_UNBOUND_ERROR = (
    "error: MOZYO_PROVIDER_ARGV0 did not verify as a trusted alias bound to the "
    "provider exec target (an absolute exec-target realpath named by an absolute "
    "same-file alias); refusing to launch with an unverified argv[0]"
)

# `die` / `warn` are the only ways this CLI writes to stderr, and both prefix their one
# line. Everything else in a captured buffer came from the host or the interpreter.
_CLI_DIAGNOSTIC_PREFIXES = ("error: ", "warning: ")


def cli_diagnostic_lines(captured_stderr: str) -> list[str]:
    """The lines the CLI itself wrote, separated from host / interpreter stderr noise.

    Redmine #14250: ``contextlib.redirect_stderr`` captures the whole process-level
    ``sys.stderr`` for the duration of the block, so an interpreter warning that happens
    to fire during the call under test (``warnings.warn`` writes through ``sys.stderr``,
    once per location — hence order-dependent and invisible to a focused run) lands in
    the same buffer. Asserting equality on that buffer made an execution-environment
    artifact part of the verdict and turned the full suite non-deterministically red.

    So classify instead of compare-everything: return only the CLI's own prefixed
    diagnostic lines, in order. Callers still assert **exact list equality** against the
    expected message, so the fail-closed contract is not loosened one character — a
    reworded, extra, missing, or duplicated CLI diagnostic is still red. What is excluded
    is only what the CLI did not write. Noise that does start with a CLI prefix is kept
    deliberately: this filter may never make the assertion weaker than it looks.
    """
    return [
        line
        for line in captured_stderr.splitlines()
        if line.startswith(_CLI_DIAGNOSTIC_PREFIXES)
    ]


def _runner(*rows):
    """A fake subprocess runner returning ``rows`` as an `agent list` JSON payload."""
    import json as _json

    def _run(argv, **kwargs):
        return argparse.Namespace(returncode=0, stdout=_json.dumps(list(rows)))

    return _run


def _failing_runner(exc=OSError("herdr down")):
    def _run(argv, **kwargs):
        raise exc

    return _run


class PerformSelfAttestationTest(unittest.TestCase):
    def setUp(self) -> None:
        # The real resolver verifies the binary exists and is executable; these tests
        # inject the subprocess runner instead, so the resolution itself is stubbed.
        patcher = patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matching_env_records_present_with_self_resolved_locator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                runner=_runner({"name": NAME, "pane_id": "wY:p2"}),
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
                runner=_runner({"name": NAME, "pane_id": "wY:p2"}),
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
                runner=_runner({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.verdict, VERDICT_CONFLICT)

    def test_ambiguous_self_lookup_writes_nothing_redmine_14231(self) -> None:
        # Redmine #14231 (j#84865): two rows with this name -> no exact locator, so NO
        # attestation record is written at all (an empty-locator record is not a valid
        # exact identity). The failure is carried by the action's event projection.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events = []
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                runner=_runner(
                    {"name": NAME, "pane_id": "wY:p2"},
                    {"name": NAME, "pane_id": "wZ:p9"},
                ),
                home=home,
                append_event=lambda stage, bounded_reason="": events.append(
                    (stage, bounded_reason)
                ),
            )
            self.assertIsNone(rec)
            self.assertIsNone(HerdrIdentityAttestationStore(home=home).read(NAME))
            self.assertIn(
                ("self_lookup_failed", "row_ambiguous"), events
            )
            self.assertIn(
                ("attestation_write_failed", "locator_unavailable"), events
            )

    def test_lookup_failure_writes_nothing_and_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            events = []
            rec = perform_self_attestation(
                assigned_name=NAME,
                workspace_id="ws1",
                role="claude",
                lane="default",
                env=_GOOD_ENV,
                runner=_failing_runner(),
                home=home,
                append_event=lambda stage, bounded_reason="": events.append(
                    (stage, bounded_reason)
                ),
            )
            self.assertIsNone(rec)
            self.assertIsNone(HerdrIdentityAttestationStore(home=home).read(NAME))
            self.assertIn(
                ("attestation_write_failed", "locator_unavailable"), events
            )


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
                runner=_runner({"name": NAME, "pane_id": "wY:p2"}),
                home=home,
            )
            self.assertEqual(rec.replacement_action_id, "recover:l:worker:claude:wk:w2")

    def test_normal_launch_records_empty_action_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rec = perform_self_attestation(
                assigned_name=NAME, workspace_id="ws1", role="claude", lane="default",
                env=_GOOD_ENV, runner=_runner({"name": NAME, "pane_id": "wY:p2"}),
                home=Path(tmp),
            )
            self.assertEqual(rec.replacement_action_id, "")


class BoundedSelfLookupTest(unittest.TestCase):
    """The pre-exec self-lookup: terminal isolation (#14017) + total budget (#14231).

    The wrapper runs inside the herdr-spawned pane and is about to ``exec`` the
    interactive provider into that same pane. Its ``herdr agent list`` child is kept
    off the pane's controlling terminal on every fd, so it can never perturb the
    stdin / foreground state the provider inherits. Redmine #14231 additionally bounds
    the WHOLE lookup to :data:`SELF_LOOKUP_TOTAL_BUDGET_SECONDS` on an injected
    monotonic clock — no real sleeping, no real subprocess.
    """

    _ENV = {"MOZYO_HERDR_BINARY": "/x/herdr", "PATH": "/usr/bin"}

    def _lookup(self, run_mock, monotonic=None, budget=SELF_LOOKUP_TOTAL_BUDGET_SECONDS):
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.resolve_herdr_binary",
            return_value=argparse.Namespace(path="/x/herdr"),
        ):
            return bounded_self_lookup(
                NAME,
                self._ENV,
                runner=run_mock,
                monotonic=monotonic or (lambda: 0.0),
                total_budget_seconds=budget,
            )

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

        locator, stage, reason = self._lookup(_run)
        self.assertEqual(locator, "wY:p2")
        self.assertEqual(stage, "self_lookup_succeeded")
        self.assertEqual(reason, "")

    def test_total_budget_is_never_exceeded_by_repeated_row_absent(self) -> None:
        # Redmine #14231 j#84743: row_absent is the ONLY retried case, and the whole
        # retry loop must fit inside the 2s budget. Fake monotonic advances 0.5s per
        # attempt, so the 5th call would be at 2.0s -> the loop must stop before it.
        clock = {"t": 0.0}
        calls = []

        def _monotonic():
            return clock["t"]

        def _run(argv, **kwargs):
            calls.append(kwargs.get("timeout"))
            clock["t"] += 0.5
            return argparse.Namespace(returncode=0, stdout="[]")  # zero matches

        locator, stage, reason = self._lookup(_run, monotonic=_monotonic)
        self.assertEqual(locator, "")
        self.assertEqual(stage, "self_lookup_timed_out")
        self.assertEqual(reason, "row_absent")
        self.assertEqual(len(calls), 4)  # 0.0/0.5/1.0/1.5 -> the 2.0s check stops it
        # Every attempt's own timeout was capped to the REMAINING budget, so no single
        # call could outlive it (the pre-#14231 bug was a fixed 10s per attempt).
        self.assertEqual(calls, [2.0, 1.5, 1.0, 0.5])

    def test_slow_success_inside_budget_still_succeeds(self) -> None:
        clock = {"t": 0.0}

        def _monotonic():
            return clock["t"]

        payloads = ["[]", '[{"name": "' + NAME + '", "pane_id": "wY:p2"}]']

        def _run(argv, **kwargs):
            clock["t"] += 0.9
            return argparse.Namespace(returncode=0, stdout=payloads.pop(0))

        locator, stage, _ = self._lookup(_run, monotonic=_monotonic)
        self.assertEqual(locator, "wY:p2")
        self.assertEqual(stage, "self_lookup_succeeded")

    def test_unreadable_read_fails_immediately_without_burning_budget(self) -> None:
        calls = []

        def _run(argv, **kwargs):
            calls.append(1)
            return argparse.Namespace(returncode=1, stdout="")  # non-zero exit

        locator, stage, reason = self._lookup(_run)
        self.assertEqual(locator, "")
        self.assertEqual(stage, "self_lookup_failed")
        self.assertEqual(reason, "list_unreadable")
        self.assertEqual(len(calls), 1)  # not retried: a retry cannot fix it

    def test_ambiguous_row_fails_immediately(self) -> None:
        def _run(argv, **kwargs):
            return argparse.Namespace(
                returncode=0,
                stdout='[{"name": "' + NAME + '", "pane_id": "wY:p2"}, '
                '{"name": "' + NAME + '", "pane_id": "wZ:p9"}]',
            )

        _, stage, reason = self._lookup(_run)
        self.assertEqual(stage, "self_lookup_failed")
        self.assertEqual(reason, "row_ambiguous")

    def test_unresolved_binary_fails_without_running_anything(self) -> None:
        def _explode(argv, **kwargs):
            raise AssertionError("must not run a subprocess with no binary")

        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_agent_attest.resolve_herdr_binary",
            side_effect=TerminalTransportError("unresolved"),
        ):
            locator, stage, reason = bounded_self_lookup(
                NAME, self._ENV, runner=_explode, monotonic=lambda: 0.0
            )
        self.assertEqual(locator, "")
        self.assertEqual(stage, "self_lookup_failed")
        self.assertEqual(reason, "binary_unresolved")


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
            "application.herdr_agent_attest.bounded_self_lookup",
            return_value=("wY:p2", "self_lookup_succeeded", ""),
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
            "application.herdr_agent_attest.bounded_self_lookup",
            return_value=("wY:p2", "self_lookup_succeeded", ""),
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
        stderr = io.StringIO()
        with patch("os.execvp") as execvp, contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cmd_herdr_agent_attest(self._args([]))
            execvp.assert_not_called()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            cli_diagnostic_lines(stderr.getvalue()), [MISSING_PROVIDER_ARGV_ERROR]
        )


def _install_real_exe(directory: str, name: str) -> str:
    """A real executable file at ``<directory>/<name>``, returning its realpath."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return os.path.realpath(path)


class Argv0AliasBindingTest(unittest.TestCase):
    """The wrapper-side fail-closed alias->exec-target binding predicate (R3-F1).

    The wrapper is a separate trust boundary from the resolver, so it re-verifies the
    binding at exec time: the exec target must be an absolute realpath of its own and the
    absolute alias must name that same file. Every other shape is rejected value-free.
    """

    def test_symlink_alias_to_realpath_target_binds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            alias = os.path.join(base, "claude")
            os.symlink(real, alias)
            self.assertTrue(_argv0_alias_binds_to_exec_target(alias, real))

    def test_equal_absolute_realpath_binds(self) -> None:
        # Unsymlinked provider: alias == exec-target realpath (byte-invariant form).
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "codex")
            self.assertTrue(_argv0_alias_binds_to_exec_target(real, real))

    def test_relative_alias_does_not_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            self.assertFalse(_argv0_alias_binds_to_exec_target("claude", real))
            self.assertFalse(_argv0_alias_binds_to_exec_target("./claude", real))

    def test_nonexistent_alias_does_not_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            self.assertFalse(
                _argv0_alias_binds_to_exec_target(os.path.join(base, "missing"), real)
            )

    def test_unrelated_absolute_alias_does_not_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            other = _install_real_exe(base, "unrelated")
            self.assertFalse(_argv0_alias_binds_to_exec_target(other, real))

    def test_different_target_symlink_does_not_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            other_real = _install_real_exe(base, "other-real")
            other_alias = os.path.join(base, "other-alias")
            os.symlink(other_real, other_alias)
            self.assertFalse(_argv0_alias_binds_to_exec_target(other_alias, real))

    def test_non_realpath_exec_target_is_not_an_anchor(self) -> None:
        # The exec target must be its OWN realpath; a symlink exec target is not a
        # canonical resolver output and cannot anchor a binding even for an alias that
        # resolves to the same underlying file.
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            symlinked_target = os.path.join(base, "claude-symlink")
            os.symlink(real, symlinked_target)
            self.assertFalse(
                _argv0_alias_binds_to_exec_target(symlinked_target, symlinked_target)
            )

    def test_relative_exec_target_is_not_an_anchor(self) -> None:
        self.assertFalse(_argv0_alias_binds_to_exec_target("/abs/alias", "relative/exe"))


class CmdAgentAttestArgv0DecouplingTest(unittest.TestCase):
    """Redmine #14017: exec the realpath, present the trusted alias as argv[0].

    The exec target is always ``provider_argv[0]`` (the verified realpath); the alias,
    when the launch injected ``MOZYO_PROVIDER_ARGV0``, is applied as argv[0] DATA only —
    via ``os.execv`` (explicit path, no PATH search) so the alias itself is never run.
    R3-F1: the wrapper re-verifies the alias->exec-target binding fail-closed at exec
    time, so a set-but-unbound alias never reaches argv[0]; it dies typed/value-free
    instead of launching (no silent realpath-argv[0] fallback).
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
            "application.herdr_agent_attest.bounded_self_lookup",
            return_value=("wY:p2", "self_lookup_succeeded", ""),
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

    def test_bound_symlink_alias_execs_realpath_with_alias_argv0(self) -> None:
        # Positive: a REAL temp symlink alias that resolves TO the realpath exec target.
        # The wrapper execs the realpath but presents the trusted alias as argv[0].
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)  # canonical: exec target is its own realpath
            real = _install_real_exe(base, "claude-real")
            alias = os.path.join(base, "claude")
            os.symlink(real, alias)
            execv, execvp, leftover = self._run(
                ["--", real, "--permission-mode", "auto"],
                {MOZYO_PROVIDER_ARGV0_ENV: alias},
            )
        execv.assert_called_once_with(real, [alias, "--permission-mode", "auto"])
        execvp.assert_not_called()
        self.assertIsNone(leftover)  # dropped from the provider's inherited env

    def test_no_alias_env_execvp_byte_invariant(self) -> None:
        execv, execvp, _ = self._run(
            ["--", "/opt/codex", "-c", "x=1"], {}
        )
        execvp.assert_called_once_with("/opt/codex", ["/opt/codex", "-c", "x=1"])
        execv.assert_not_called()

    def _assert_alias_fails_closed(self, provider_argv, alias) -> None:
        # A set-but-unbound alias dies typed/value-free: NEITHER exec runs (no launch),
        # and the value is dropped from the env even on the failure path. The diagnostic
        # is asserted structurally (#14250): exactly one CLI line, exactly this text —
        # host / interpreter stderr that shares the capture buffer is not part of the
        # verdict. See :func:`cli_diagnostic_lines`.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            execv, execvp, leftover = self._run(
                provider_argv, {MOZYO_PROVIDER_ARGV0_ENV: alias}
            )
        execv.assert_not_called()
        execvp.assert_not_called()
        self.assertIsNone(leftover)
        self.assertEqual(
            cli_diagnostic_lines(stderr.getvalue()), [ARGV0_ALIAS_UNBOUND_ERROR]
        )

    def test_relative_alias_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = _install_real_exe(os.path.realpath(tmp), "claude-real")
            self._assert_alias_fails_closed(["--", real], "claude")

    def test_unrelated_absolute_alias_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            other = _install_real_exe(base, "unrelated")
            self._assert_alias_fails_closed(["--", real], other)

    def test_nonexistent_alias_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            self._assert_alias_fails_closed(["--", real], os.path.join(base, "missing"))

    def test_different_target_symlink_alias_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            real = _install_real_exe(base, "claude-real")
            other_real = _install_real_exe(base, "other-real")
            other_alias = os.path.join(base, "other-alias")
            os.symlink(other_real, other_alias)
            self._assert_alias_fails_closed(["--", real], other_alias)


if __name__ == "__main__":
    unittest.main()
