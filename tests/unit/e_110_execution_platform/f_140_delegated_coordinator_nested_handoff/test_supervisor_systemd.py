"""Linux systemd user service+timer lifecycle tests for the callback supervisor (Redmine #15183).

Real ``systemctl`` is never invoked and the real host unit directory is never touched: every
subprocess call goes through an injected fake runner, and temp roots stand in for the OS user home
(``os_home``: unit files) and the mozyo home (``mozyo_home``: credential/registry root) — two roots
that are kept **distinct** (the launchd adapter's review j#79092 R2-F1 applies unchanged). These pin
the Linux adapter's safety boundary —

- unit structure: ``Type=oneshot`` with **no** ``Restart=`` / ``RemainAfterExit=`` (the KeepAlive
  equivalent is structurally absent), **no** ``Environment=`` / ``EnvironmentFile=``, no
  ``[Install]`` on the service (only the timer is enabled), and ``OnActiveSec=0s`` +
  ``OnUnitActiveSec=<N>s`` as the RunAtLoad + StartInterval equivalent;
- ``ExecStart`` is the exact PATH-resolved absolute executable argv with the resolved mozyo home
  pinned as ``--home``, systemd-quoted per token and round-tripping back to the same argv;
- structured ``systemctl --user`` argv (daemon-reload + enable --now install, restart on the
  service, disable --now + file removal uninstall), idempotent install;
- fail-closed **zero-mutation** refusals: non-Linux host, no reachable systemd user manager (the
  container case is unsupported, never a silent degrade), missing executable, and the Redmine
  credential matrix — daemon-effective readiness (neither a shell key/URL nor a shell
  ``MOZYO_BRIDGE_HOME`` can make it ``ready``);
- the installed unit's ``--home`` pin is the authority for restart / status — never the caller's
  current shell — and a drifted installed command is never re-run;
- atomic-or-nothing pair install (a drain failure rolls the reconcile pair back);
- a redacted status projection (booleans / counts / fixed tokens; no secret, no path).

without touching the host. Live systemd operation is recorded as a separate installed-artifact
smoke on the issue (never here).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_service_backend as sb,
    supervisor_systemd as ss,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    API_KEY_ENV,
    BASE_URL_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    credentials_path,
)

#: A shell env that WOULD look ready on the interactive path — but a systemd unit never sees it.
SHELL_ENV = {API_KEY_ENV: "shell-key-sentinel", BASE_URL_ENV: "https://redmine.shell.test"}


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def _write_home_credential(
    mozyo_home: Path, *, api_key="home-key-sentinel", url="https://redmine.example.test", mode=0o600
) -> Path:
    """Write a mozyo-home-scoped `redmine-credentials.yaml` — the daemon-trusted delivery path."""
    cred = credentials_path(mozyo_home)
    cred.parent.mkdir(parents=True, exist_ok=True)
    body = "redmine:\n"
    if api_key is not None:
        body += f"  api_key: {api_key}\n"
    if url is not None:
        body += f"  url: {url}\n"
    cred.write_text(body, encoding="utf-8")
    os.chmod(cred, mode)
    return cred


class FakeRunner:
    """Records every structured argv and returns a scripted (or default-ok) result.

    ``show_map`` scripts ``systemctl --user show <unit> ...`` per unit name; ``fail_verbs`` scripts a
    non-zero result for a given systemctl verb. Everything else succeeds.
    """

    def __init__(self, *, show_map=None, fail_verbs=(), manager_available=True) -> None:
        self.calls: list[list[str]] = []
        self._show_map = show_map or {}
        self._fail_verbs = set(fail_verbs)
        self._manager_available = manager_available

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        # argv is always ["systemctl", "--user", <verb>, ...]
        verb = argv[2] if len(argv) > 2 else ""
        if verb == "show":
            if any(a.startswith("--property=Version") for a in argv):
                return _result(0 if self._manager_available else 1)
            unit = argv[3] if len(argv) > 3 else ""
            return _result(0, self._show_map.get(unit, ""))
        if verb in self._fail_verbs:
            return _result(1, stderr="redacted")
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls if len(c) > 2]


def _timer_show(*, active="active", enabled="enabled") -> str:
    return f"ActiveState={active}\nUnitFileState={enabled}\n"


def _which_found(_name: str):
    return "/opt/bin/mozyo-bridge"


def _which_missing(_name: str):
    return None


def _which_relative(_name: str):
    # A relative PATH entry makes shutil.which return a relative path (the launchd R5-F1 case).
    return "bin/mozyo-bridge"


class _LinuxCase(unittest.TestCase):
    """Base: force Linux and provide distinct os_home / mozyo_home temp roots."""

    def setUp(self) -> None:
        self._tmp_os = tempfile.TemporaryDirectory()
        self._tmp_mozyo = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_os.cleanup)
        self.addCleanup(self._tmp_mozyo.cleanup)
        self.os_home = Path(self._tmp_os.name)
        self.mozyo_home = Path(self._tmp_mozyo.name)
        patcher = patch.object(sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ready_runner(self, **kwargs) -> FakeRunner:
        """A runner whose owned timers report active+enabled (the installed-and-scheduled case)."""
        show_map = kwargs.pop("show_map", None) or {
            u.timer_unit: _timer_show() for u in ss.SUPERVISOR_UNITS
        }
        return FakeRunner(show_map=show_map, **kwargs)

    def _install_reconcile(self, *, runner=None, interval=300) -> dict:
        _write_home_credential(self.mozyo_home)
        return ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=interval,
            runner=runner or self._ready_runner(), which=_which_found,
        )


# ---------------------------------------------------------------------------
# Unit rendering: the secret-free, no-relaunch-loop, one-shot structure.
# ---------------------------------------------------------------------------


class UnitRenderingTest(_LinuxCase):
    def _service_text(self) -> str:
        command = ss.resolve_supervisor_command(mozyo_home=self.mozyo_home, which=_which_found)
        return ss.render_service_unit(command)

    def test_the_service_unit_is_a_one_shot_with_no_relaunch_directive(self) -> None:
        text = self._service_text()
        self.assertIn("Type=oneshot", text)
        # The KeepAlive equivalent must be structurally ABSENT, not set to a falsy value: a
        # Restart= / RemainAfterExit= on a one-shot is a tight relaunch loop.
        self.assertNotIn("Restart=", text)
        self.assertNotIn("RemainAfterExit=", text)

    def test_the_service_unit_carries_no_environment_block(self) -> None:
        text = self._service_text()
        self.assertNotIn("Environment=", text)
        self.assertNotIn("EnvironmentFile=", text)

    def test_the_service_unit_has_no_install_section(self) -> None:
        # Only the TIMER is enabled. A directly enabled service would run once at login and never
        # again, silently replacing the cadence.
        self.assertNotIn("[Install]", self._service_text())

    def test_the_service_execstart_is_the_exact_pinned_argv(self) -> None:
        command = ss.resolve_supervisor_command(mozyo_home=self.mozyo_home, which=_which_found)
        self.assertEqual(
            command,
            [
                "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
                "--home", str(ss.resolve_mozyo_home(self.mozyo_home)),
            ],
        )
        line = [
            ln for ln in ss.render_service_unit(command).splitlines()
            if ln.startswith("ExecStart=")
        ][0]
        self.assertEqual(ss.parse_exec_argv(line[len("ExecStart="):]), command)

    def test_the_execstart_value_is_never_a_shell_invocation(self) -> None:
        text = self._service_text()
        self.assertNotIn("/bin/sh", text)
        self.assertNotIn("-c ", text)

    def test_the_drain_unit_renders_the_drain_argv_tail(self) -> None:
        command = ss.resolve_supervisor_command(
            mozyo_home=self.mozyo_home, which=_which_found, unit=ss.DRAIN_UNIT
        )
        self.assertEqual(command[1:4], ["workflow", "supervisor", "--drain-only"])

    def test_the_timer_pairs_run_at_load_with_the_fixed_interval(self) -> None:
        text = ss.render_timer_unit(interval_seconds=300)
        self.assertIn(f"OnActiveSec={ss.RUN_AT_LOAD_DELAY}", text)  # RunAtLoad equivalent
        self.assertIn("OnUnitActiveSec=300s", text)  # StartInterval equivalent
        self.assertIn("Unit=mozyo-bridge-callback-supervisor.service", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_the_timer_declares_no_calendar_catch_up(self) -> None:
        # There is no missed-run to replay: the next tick reconciles whatever the last one missed.
        text = ss.render_timer_unit(interval_seconds=300)
        self.assertNotIn("OnCalendar=", text)
        self.assertNotIn("Persistent=", text)

    def test_a_non_positive_interval_is_floored_to_one_second(self) -> None:
        self.assertIn("OnUnitActiveSec=1s", ss.render_timer_unit(interval_seconds=0))
        self.assertIn("OnUnitActiveSec=1s", ss.render_timer_unit(interval_seconds=-5))

    def test_the_two_owned_pairs_have_distinct_unit_names(self) -> None:
        names = {u.service_unit for u in ss.SUPERVISOR_UNITS} | {
            u.timer_unit for u in ss.SUPERVISOR_UNITS
        }
        self.assertEqual(len(names), 4)
        self.assertEqual(len(ss.SUPERVISOR_UNITS), 2)  # reconcile + drain only; no third cadence


class ExecArgvQuotingTest(unittest.TestCase):
    """systemd splits ExecStart on whitespace, so every token must survive a quote round-trip."""

    def test_paths_with_spaces_round_trip(self) -> None:
        argv = ["/opt/my bin/mozyo-bridge", "workflow", "--home", "/home/a b/.mozyo_bridge"]
        self.assertEqual(ss.parse_exec_argv(ss.format_exec_argv(argv)), argv)

    def test_quotes_and_backslashes_round_trip(self) -> None:
        argv = ['/opt/we"ird/mozyo-bridge', "--home", "/home/back\\slash"]
        self.assertEqual(ss.parse_exec_argv(ss.format_exec_argv(argv)), argv)

    def test_an_unterminated_quote_is_not_trusted(self) -> None:
        self.assertIsNone(ss.parse_exec_argv('"/opt/bin/mozyo-bridge'))

    def test_a_bare_hand_written_value_still_reads_back(self) -> None:
        self.assertEqual(
            ss.parse_exec_argv("/opt/bin/mozyo-bridge workflow supervisor"),
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor"],
        )

    def test_a_systemd_command_prefix_is_reported_as_drift_not_normalized(self) -> None:
        # This adapter never writes ``-`` / ``@`` / ``:`` / ``!`` prefixes; one present must not be
        # silently stripped into a matching command.
        self.assertEqual(ss.parse_exec_argv("-/opt/bin/mozyo-bridge"), ["-/opt/bin/mozyo-bridge"])


# ---------------------------------------------------------------------------
# Unit directory resolution: write where the user manager actually reads.
# ---------------------------------------------------------------------------


class UnitDirectoryTest(_LinuxCase):
    def test_an_explicit_os_home_uses_the_xdg_default_under_it(self) -> None:
        self.assertEqual(ss.unit_dir(self.os_home), self.os_home / ".config/systemd/user")

    def test_an_absolute_xdg_config_home_is_honoured(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/cfg"}, clear=False):
            self.assertEqual(ss.unit_dir(), Path("/custom/cfg/systemd/user"))

    def test_a_relative_xdg_config_home_falls_back_to_the_home_default(self) -> None:
        # A relative XDG value is not something the user manager resolves from our cwd.
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/cfg"}, clear=False):
            self.assertEqual(ss.unit_dir(), Path.home() / ".config/systemd/user")


# ---------------------------------------------------------------------------
# Install: fail-closed zero-mutation refusals, then the structured systemctl sequence.
# ---------------------------------------------------------------------------


class InstallRefusalTest(_LinuxCase):
    def _assert_zero_mutation(self, runner: FakeRunner) -> None:
        self.assertFalse(ss.unit_dir(self.os_home).exists())
        self.assertEqual([v for v in runner.verbs if v != "show"], [])

    def test_a_non_linux_host_refuses_before_any_mutation(self) -> None:
        runner = self._ready_runner()
        with patch.object(sys, "platform", "darwin"):
            result = ss.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
            )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_UNSUPPORTED_PLATFORM)
        self._assert_zero_mutation(runner)

    def test_an_unreachable_user_manager_refuses_before_any_mutation(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = self._ready_runner(manager_available=False)
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_USER_MANAGER_UNAVAILABLE)
        self._assert_zero_mutation(runner)

    def test_an_absent_systemctl_reads_as_no_user_manager(self) -> None:
        def exploding(_argv):
            raise FileNotFoundError("systemctl")

        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=exploding, which=_which_found
        )
        self.assertEqual(result["reason"], ss.REASON_USER_MANAGER_UNAVAILABLE)
        self.assertFalse(ss.unit_dir(self.os_home).exists())

    def test_a_missing_executable_refuses_before_any_mutation(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = self._ready_runner()
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_missing
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_EXECUTABLE_NOT_FOUND)
        self._assert_zero_mutation(runner)

    def test_a_missing_credential_refuses_before_any_mutation(self) -> None:
        runner = self._ready_runner()
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], "redmine_credential_missing")
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)
        self._assert_zero_mutation(runner)

    def test_an_incomplete_credential_refuses(self) -> None:
        _write_home_credential(self.mozyo_home, url=None)
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_INCOMPLETE)
        self.assertEqual(result["reason"], "redmine_credential_incomplete")

    def test_an_unsafe_credential_file_refuses(self) -> None:
        _write_home_credential(self.mozyo_home, mode=0o644)
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_UNSAFE)
        self.assertEqual(result["reason"], "redmine_credential_unsafe")

    def test_a_shell_environment_can_never_make_readiness_ready(self) -> None:
        # A systemd unit carries no Environment block and inherits no interactive shell, so the
        # installer's exported MOZYO_REDMINE_* must not produce a false ``ready``.
        with patch.dict(os.environ, SHELL_ENV, clear=False):
            result = ss.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=self._ready_runner(), which=_which_found,
            )
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)
        self.assertFalse(result["performed"])

    def test_a_shell_mozyo_bridge_home_can_never_redirect_the_readiness_root(self) -> None:
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        _write_home_credential(other)  # a READY credential in a DIFFERENT root
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(other)}, clear=False):
            result = ss.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=self._ready_runner(), which=_which_found,
            )
        # The explicit --home root is what the unit pins, so ITS readiness decides.
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)


class InstallSuccessTest(_LinuxCase):
    def test_install_writes_both_units_then_reloads_and_enables_the_timer(self) -> None:
        runner = self._ready_runner()
        result = self._install_reconcile(runner=runner)
        self.assertTrue(result["performed"], result)
        self.assertEqual(result["scheduled_interval_seconds"], 300)
        self.assertTrue(ss.service_unit_path(self.os_home).exists())
        self.assertTrue(ss.timer_unit_path(self.os_home).exists())
        mutating = [c for c in runner.calls if c[2] != "show"]
        self.assertEqual(
            mutating,
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "mozyo-bridge-callback-supervisor.timer"],
            ],
        )

    def test_install_is_idempotent(self) -> None:
        self._install_reconcile()
        first = ss.service_unit_path(self.os_home).read_text(encoding="utf-8")
        self._install_reconcile()
        self.assertEqual(ss.service_unit_path(self.os_home).read_text(encoding="utf-8"), first)

    def test_the_installed_unit_pins_the_absolute_canonical_mozyo_home(self) -> None:
        self._install_reconcile()
        text = ss.service_unit_path(self.os_home).read_text(encoding="utf-8")
        self.assertIn(str(self.mozyo_home.resolve()), text)

    def test_a_relative_executable_is_pinned_as_an_absolute_path(self) -> None:
        # A relative PATH entry would be resolved from the systemd process's cwd, not the installer's.
        _write_home_credential(self.mozyo_home)
        ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_relative,
        )
        argv = ss.parse_exec_argv(
            [
                ln for ln in ss.service_unit_path(self.os_home).read_text().splitlines()
                if ln.startswith("ExecStart=")
            ][0][len("ExecStart="):]
        )
        self.assertTrue(os.path.isabs(argv[0]), argv)

    def test_a_failed_daemon_reload_reports_a_redacted_token(self) -> None:
        runner = self._ready_runner(fail_verbs=("daemon-reload",))
        result = self._install_reconcile(runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_DAEMON_RELOAD_FAILED)
        self.assertNotIn("enable", runner.verbs)

    def test_a_failed_enable_reports_a_redacted_token(self) -> None:
        runner = self._ready_runner(fail_verbs=("enable",))
        result = self._install_reconcile(runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_ENABLE_FAILED)


# ---------------------------------------------------------------------------
# Restart: the installed unit is the authority; never re-run a drifted command.
# ---------------------------------------------------------------------------


class RestartTest(_LinuxCase):
    def test_restart_runs_the_service_when_the_owned_timer_is_active(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(result["performed"], result)
        self.assertIn(
            ["systemctl", "--user", "restart", "mozyo-bridge-callback-supervisor.service"],
            runner.calls,
        )

    def test_restart_refuses_when_nothing_is_installed(self) -> None:
        runner = self._ready_runner()
        result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_NOT_INSTALLED)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_when_the_owned_timer_is_not_active(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner(
            show_map={u.timer_unit: _timer_show(active="inactive") for u in ss.SUPERVISOR_UNITS}
        )
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertEqual(result["reason"], ss.REASON_SERVICE_NOT_LOADED)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_an_unreadable_service_unit(self) -> None:
        self._install_reconcile()
        ss.service_unit_path(self.os_home).write_text("not a unit at all\n", encoding="utf-8")
        runner = self._ready_runner()
        result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_UNHEALTHY)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_UNREADABLE)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_a_unit_with_two_execstart_lines(self) -> None:
        self._install_reconcile()
        target = ss.service_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8") + 'ExecStart="/opt/bin/other"\n', encoding="utf-8"
        )
        result = ss.restart(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertEqual(result["home_pin"], ss.HOME_PIN_UNREADABLE)

    def test_restart_refuses_a_missing_home_pin(self) -> None:
        self._install_reconcile()
        target = ss.service_unit_path(self.os_home)
        target.write_text(
            ss.render_service_unit(["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"]),
            encoding="utf-8",
        )
        result = ss.restart(os_home=self.os_home, runner=self._ready_runner(), which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_UNHEALTHY)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_MISSING)

    def test_restart_refuses_a_relative_home_pin(self) -> None:
        self._install_reconcile()
        ss.service_unit_path(self.os_home).write_text(
            ss.render_service_unit(
                ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", "rel/root"]
            ),
            encoding="utf-8",
        )
        result = ss.restart(os_home=self.os_home, runner=self._ready_runner(), which=_which_found)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_NOT_ABSOLUTE)

    def test_restart_refuses_a_requested_home_that_disagrees_with_the_pin(self) -> None:
        self._install_reconcile()
        other = Path(tempfile.mkdtemp())
        runner = self._ready_runner()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=other, runner=runner, which=_which_found
        )
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_MISMATCH)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_a_drifted_installed_command(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner()
        result = ss.restart(
            os_home=self.os_home, runner=runner, which=lambda _n: "/somewhere/else/mozyo-bridge"
        )
        self.assertEqual(result["reason"], ss.REASON_INSTALLED_COMMAND_DRIFT)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_judges_readiness_against_the_pinned_home_not_the_caller_shell(self) -> None:
        self._install_reconcile()
        credentials_path(self.mozyo_home).unlink()  # the PINNED root is no longer ready
        ready_elsewhere = Path(tempfile.mkdtemp())
        _write_home_credential(ready_elsewhere)
        runner = self._ready_runner()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(ready_elsewhere)}, clear=False):
            result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)
        self.assertNotIn("restart", runner.verbs)

    def test_a_failed_restart_reports_a_redacted_token(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner(fail_verbs=("restart",))
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_RESTART_FAILED)


# ---------------------------------------------------------------------------
# Uninstall: no credential required; removes exactly the owned files.
# ---------------------------------------------------------------------------


class UninstallTest(_LinuxCase):
    def test_uninstall_disables_stops_and_removes_exactly_the_owned_units(self) -> None:
        self._install_reconcile()
        foreign = ss.unit_dir(self.os_home) / "someone-elses.service"
        foreign.write_text("[Unit]\n", encoding="utf-8")
        runner = self._ready_runner()
        result = ss.uninstall(os_home=self.os_home, runner=runner)
        self.assertTrue(result["performed"])
        self.assertTrue(result["removed"])
        self.assertFalse(ss.service_unit_path(self.os_home).exists())
        self.assertFalse(ss.timer_unit_path(self.os_home).exists())
        self.assertTrue(foreign.exists())  # untouched
        self.assertEqual(
            [c for c in runner.calls if c[2] != "show"],
            [
                ["systemctl", "--user", "disable", "--now", "mozyo-bridge-callback-supervisor.timer"],
                ["systemctl", "--user", "stop", "mozyo-bridge-callback-supervisor.service"],
                ["systemctl", "--user", "daemon-reload"],
                [
                    "systemctl", "--user", "reset-failed",
                    "mozyo-bridge-callback-supervisor.service",
                    "mozyo-bridge-callback-supervisor.timer",
                ],
            ],
        )

    def test_uninstall_clears_the_manager_side_failed_record(self) -> None:
        # Measured live (Redmine #15183 smoke): ``stop`` on a mid-flight sweep SIGTERMs it, so the
        # one-shot exits on a signal and systemd keeps a ``failed`` record that survives the file
        # removal as a ``not-found``/``failed`` entry in ``list-units``. launchd's ``bootout``
        # leaves no such trace, so uninstall must clear the manager state too.
        self._install_reconcile()
        runner = self._ready_runner()
        ss.uninstall(os_home=self.os_home, runner=runner)
        reset = [c for c in runner.calls if c[2] == "reset-failed"]
        self.assertEqual(len(reset), 1)
        self.assertEqual(
            reset[0][3:],
            [ss.RECONCILE_UNIT.service_unit, ss.RECONCILE_UNIT.timer_unit],
        )

    def test_the_manager_state_is_cleared_after_the_files_are_gone(self) -> None:
        # Ordering matters: a reset-failed BEFORE daemon-reload would be re-dirtied by the reload
        # re-reading a still-present unit. It must be the last step.
        self._install_reconcile()
        runner = self._ready_runner()
        ss.uninstall(os_home=self.os_home, runner=runner)
        verbs = [c[2] for c in runner.calls if c[2] != "show"]
        self.assertLess(verbs.index("daemon-reload"), verbs.index("reset-failed"))

    def test_uninstall_works_with_no_credential_at_all(self) -> None:
        # You must be able to tear a service down without configured credentials.
        result = ss.uninstall(os_home=self.os_home, runner=self._ready_runner())
        self.assertTrue(result["performed"])
        self.assertFalse(result["removed"])

    def test_uninstall_refuses_on_a_non_linux_host(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            result = ss.uninstall(os_home=self.os_home, runner=self._ready_runner())
        self.assertEqual(result["reason"], ss.REASON_UNSUPPORTED_PLATFORM)


# ---------------------------------------------------------------------------
# Status: read-only, redacted, and honest about what it cannot verify.
# ---------------------------------------------------------------------------


class ServiceStatusTest(_LinuxCase):
    def test_status_projects_the_installed_and_scheduled_service(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner(
            show_map={
                ss.RECONCILE_UNIT.timer_unit: _timer_show(),
                ss.RECONCILE_UNIT.service_unit: "MainPID=4321\n",
            }
        )
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertTrue(status["timer_enabled"])
        self.assertEqual(status["pid"], 4321)
        self.assertEqual(status["scheduled_interval_seconds"], 300)
        self.assertTrue(status["run_at_load"])
        self.assertFalse(status["keep_alive_present"])
        self.assertTrue(status["no_environment_block"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_OK)
        self.assertTrue(status["executable_matches"])
        self.assertEqual(status["credential_readiness"], ss.CREDENTIAL_READY)

    def test_status_mutates_nothing(self) -> None:
        runner = self._ready_runner()
        ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual([v for v in runner.verbs if v != "show"], [])
        self.assertFalse(ss.unit_dir(self.os_home).exists())

    def test_status_emits_only_tokens_counts_and_booleans(self) -> None:
        self._install_reconcile()
        _write_home_credential(self.mozyo_home, api_key="super-secret-key")
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        blob = repr(status)
        self.assertNotIn("super-secret-key", blob)
        self.assertNotIn("redmine.example.test", blob)

    def test_an_uninstalled_host_reports_the_would_be_root_readiness(self) -> None:
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertFalse(status["installed"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_NOT_INSTALLED)
        self.assertEqual(status["credential_readiness"], ss.CREDENTIAL_MISSING)
        self.assertEqual(status["scheduled_interval_seconds"], 300)  # the hint

    def test_a_present_but_unreadable_unit_is_distinct_from_absence(self) -> None:
        self._install_reconcile()
        ss.service_unit_path(self.os_home).write_text("garbage\n", encoding="utf-8")
        status = ss.service_status(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertEqual(status["home_pin"], ss.HOME_PIN_UNREADABLE)
        self.assertEqual(status["credential_readiness"], "")  # unknowable, never guessed
        self.assertFalse(status["executable_matches"])

    def test_a_lone_service_file_is_not_reported_as_installed(self) -> None:
        self._install_reconcile()
        ss.timer_unit_path(self.os_home).unlink()  # no cadence left
        status = ss.service_status(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertFalse(status["installed"])
        self.assertTrue(status["service_unit_exists"])
        self.assertFalse(status["timer_unit_exists"])

    def test_a_hand_added_restart_directive_surfaces_as_keep_alive_present(self) -> None:
        self._install_reconcile()
        target = ss.service_unit_path(self.os_home)
        target.write_text(target.read_text(encoding="utf-8") + "Restart=always\n", encoding="utf-8")
        status = ss.service_status(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertTrue(status["keep_alive_present"])

    def test_a_hand_added_environment_key_surfaces_as_an_environment_block(self) -> None:
        self._install_reconcile()
        target = ss.service_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8") + "Environment=FOO=bar\n", encoding="utf-8"
        )
        status = ss.service_status(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertFalse(status["no_environment_block"])

    def test_an_unparseable_cadence_is_reported_as_unknown_not_reinterpreted(self) -> None:
        self._install_reconcile()
        target = ss.timer_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8").replace("OnUnitActiveSec=300s", "OnUnitActiveSec=5min"),
            encoding="utf-8",
        )
        status = ss.service_status(
            os_home=self.os_home, runner=self._ready_runner(), which=_which_found
        )
        self.assertIsNone(status["scheduled_interval_seconds"])

    def test_a_non_ascii_pid_reads_as_none_instead_of_raising(self) -> None:
        # The Redmine #14753 defect class: ``str.isdigit()`` accepts characters ``int()`` may read
        # differently (or not at all). An unreadable pid must degrade, never raise.
        self._install_reconcile()
        runner = self._ready_runner(
            show_map={
                ss.RECONCILE_UNIT.timer_unit: _timer_show(),
                ss.RECONCILE_UNIT.service_unit: "MainPID=²\n",
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertIsNone(status["pid"])

    def test_a_zero_main_pid_reads_as_no_running_sweep(self) -> None:
        self._install_reconcile()
        runner = self._ready_runner(
            show_map={
                ss.RECONCILE_UNIT.timer_unit: _timer_show(),
                ss.RECONCILE_UNIT.service_unit: "MainPID=0\n",
            }
        )
        self.assertIsNone(
            ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)["pid"]
        )


# ---------------------------------------------------------------------------
# Pair orchestration: atomic-or-nothing install over both cadences.
# ---------------------------------------------------------------------------


class PairTest(_LinuxCase):
    def test_install_pair_installs_both_cadences(self) -> None:
        _write_home_credential(self.mozyo_home)
        result = ss.install_pair(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertTrue(result["performed"], result)
        self.assertEqual(len(result["agents"]), 2)
        for unit in ss.SUPERVISOR_UNITS:
            self.assertTrue(ss.service_unit_path(self.os_home, unit=unit).exists())
            self.assertTrue(ss.timer_unit_path(self.os_home, unit=unit).exists())

    def test_the_two_cadences_are_installed_at_their_distinct_intervals(self) -> None:
        _write_home_credential(self.mozyo_home)
        ss.install_pair(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            reconcile_interval_seconds=300, drain_interval_seconds=60,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertIn(
            "OnUnitActiveSec=300s",
            ss.timer_unit_path(self.os_home, unit=ss.RECONCILE_UNIT).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "OnUnitActiveSec=60s",
            ss.timer_unit_path(self.os_home, unit=ss.DRAIN_UNIT).read_text(encoding="utf-8"),
        )

    def test_a_refused_reconcile_install_touches_nothing_else(self) -> None:
        result = ss.install_pair(  # no credential written
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(len(result["agents"]), 1)
        self.assertFalse(ss.unit_dir(self.os_home).exists())

    def test_a_failed_drain_install_rolls_the_whole_pair_back(self) -> None:
        _write_home_credential(self.mozyo_home)

        class DrainFailingRunner(FakeRunner):
            def __call__(self, argv):
                argv = list(argv)
                if argv[2] == "enable" and "drain" in argv[-1]:
                    self.calls.append(argv)
                    return _result(1)
                return super().__call__(argv)

        runner = DrainFailingRunner(
            show_map={u.timer_unit: _timer_show() for u in ss.SUPERVISOR_UNITS}
        )
        result = ss.install_pair(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["reason"], ss.REASON_ENABLE_FAILED)
        # No half-installed set is left behind — neither cadence's units survive.
        for unit in ss.SUPERVISOR_UNITS:
            self.assertFalse(ss.service_unit_path(self.os_home, unit=unit).exists())
            self.assertFalse(ss.timer_unit_path(self.os_home, unit=unit).exists())

    def test_status_pair_projects_both_cadences_without_mutating(self) -> None:
        runner = self._ready_runner()
        status = ss.service_status_pair(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertEqual(len(status["agents"]), 2)
        self.assertEqual(
            [a["label"] for a in status["agents"]],
            [ss.SUPERVISOR_SYSTEMD_LABEL, ss.SUPERVISOR_DRAIN_SYSTEMD_LABEL],
        )
        self.assertEqual([v for v in runner.verbs if v != "show"], [])

    def test_uninstall_pair_removes_both_cadences(self) -> None:
        _write_home_credential(self.mozyo_home)
        ss.install_pair(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._ready_runner(), which=_which_found,
        )
        result = ss.uninstall_pair(os_home=self.os_home, runner=self._ready_runner())
        self.assertTrue(result["performed"])
        self.assertFalse(any(p.suffix in (".service", ".timer") for p in ss.unit_dir(self.os_home).iterdir()))


# ---------------------------------------------------------------------------
# Backend selection: one contract, the host's adapter, typed refusal otherwise.
# ---------------------------------------------------------------------------


class BackendSelectionTest(unittest.TestCase):
    def test_each_platform_resolves_to_its_owned_adapter(self) -> None:
        self.assertEqual(sb.resolve_backend_name("darwin"), sb.BACKEND_LAUNCHD)
        self.assertEqual(sb.resolve_backend_name("linux"), sb.BACKEND_SYSTEMD)
        self.assertEqual(sb.resolve_backend_name("linux2"), sb.BACKEND_SYSTEMD)
        self.assertEqual(sb.resolve_backend_name("win32"), sb.BACKEND_UNSUPPORTED)

    def test_the_resolved_modules_are_the_two_adapters(self) -> None:
        self.assertEqual(sb.resolve_backend("linux")[1].__name__.rsplit(".", 1)[-1], "supervisor_systemd")
        self.assertEqual(sb.resolve_backend("darwin")[1].__name__.rsplit(".", 1)[-1], "supervisor_launchd")
        self.assertIsNone(sb.resolve_backend("win32")[1])

    def test_an_unsupported_host_gets_a_typed_zero_mutation_refusal(self) -> None:
        result = sb.unsupported_result("install")
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sb.REASON_NO_BACKEND)
        self.assertEqual(result["agents"], [])

    def test_the_dispatched_verbs_route_to_the_hosts_adapter_and_stamp_the_backend(self) -> None:
        seen = {}

        def fake_install_pair(**kwargs):
            seen.update(kwargs)
            return {"action": "install", "performed": True, "reason": "", "agents": []}

        with patch.object(sys, "platform", "linux"), patch.object(
            ss, "install_pair", fake_install_pair
        ):
            result = sb.install_pair(mozyo_home=Path("/tmp/x"))
        self.assertTrue(result["performed"])
        self.assertEqual(result["backend"], sb.BACKEND_SYSTEMD)
        self.assertEqual(seen["mozyo_home"], Path("/tmp/x"))

    def test_the_dispatched_verbs_refuse_on_an_unsupported_host(self) -> None:
        with patch.object(sys, "platform", "win32"):
            for verb in (sb.install_pair, sb.restart_pair, sb.uninstall_pair):
                result = verb()
                self.assertFalse(result["performed"], verb)
                self.assertEqual(result["reason"], sb.REASON_NO_BACKEND, verb)
            status = sb.service_status_pair()
        self.assertEqual(status["agents"], [])
        self.assertEqual(status["backend"], sb.BACKEND_UNSUPPORTED)

    def test_the_dispatched_status_never_mutates_on_an_unsupported_host(self) -> None:
        with patch.object(sys, "platform", "win32"):
            self.assertFalse(sb.service_status_pair().get("platform_supported"))

    def test_both_adapters_expose_the_same_pair_verb_surface(self) -> None:
        # The CLI drives whichever adapter owns the host without branching, so the surfaces must
        # not drift apart.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        for verb in ("install_pair", "restart_pair", "uninstall_pair", "service_status_pair"):
            self.assertTrue(hasattr(supervisor_launchd, verb), verb)
            self.assertTrue(hasattr(ss, verb), verb)


class NoResidentDaemonTest(unittest.TestCase):
    """The adapter must never introduce a resident daemon / infinite poll (issue #15183 scope)."""

    def test_the_adapter_declares_no_always_on_directive(self) -> None:
        import inspect

        text = inspect.getsource(ss)
        for directive in ("Restart=always", "Restart=on-failure", "RemainAfterExit=yes"):
            self.assertNotIn(directive, text)

    def test_the_adapter_has_no_third_cadence(self) -> None:
        self.assertEqual(len(ss.SUPERVISOR_UNITS), 2)
        self.assertNotIn("--hibernate", __import__("inspect").getsource(ss))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
