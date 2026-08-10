"""Linux systemd user service+timer tests for the callback supervisor (Redmine #15183).

Real ``systemctl`` is never invoked and the real host unit directory is never touched: every
subprocess call goes through an injected fake runner, and temp roots stand in for the OS user home
(``os_home``: unit files) and the mozyo home (``mozyo_home``: credential/registry root) — two roots
kept **distinct**. These pin the Linux adapter's contract as the updated issue body defines it
(scope correction j#101996, review findings j#102000) —

- **one** owned service + **one** owned timer, ticking ``--run-once`` on the shared portable OS
  cadence (#15192). There is no second cadence, no ``--drain-only`` unit, and no atomic pair
  install: those were removed from the acceptance contract, and a test here fails if they come back;
- the OS tick is not a Redmine poll — the provider cadence stays the supervisor body's own ~300s
  watermark, which this adapter surfaces but never sets;
- unit structure: ``Type=oneshot`` with **no** ``Restart=`` / ``RemainAfterExit=`` (the KeepAlive
  equivalent is structurally absent), **no** ``Environment=`` / ``EnvironmentFile=``, no
  ``[Install]`` on the service (only the timer is enabled), and ``OnActiveSec=0s`` +
  ``OnUnitActiveSec=<portable default>s`` as the run-at-load + fixed-interval pair;
- ``ExecStart`` is the exact PATH-resolved absolute executable argv with the resolved mozyo home
  pinned as ``--home``, systemd-quoted per token and round-tripping back to the same argv;
- **an unconfigured / incomplete / unsafe Redmine credential does NOT block installing the timer**
  (review finding 2). Readiness is projected, never gated, so local-only work keeps running;
- status is non-destructive and answers the required observations: installed / enabled state, the
  **next run**, the **last exit result**, and the **installed command** (review finding 3);
- fail-closed zero-mutation refusals remain for a non-Linux host, an unreachable systemd user
  manager (the container case is unsupported, never a silent degrade), and a missing executable;
- the installed unit's ``--home`` pin is the authority for restart / status, and a drifted installed
  command is never re-run.

Live systemd operation is recorded as a separate installed-artifact smoke on the issue (never here).
"""

from __future__ import annotations

import inspect
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
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
        verb = argv[2] if len(argv) > 2 else ""  # argv is ["systemctl", "--user", <verb>, ...]
        if verb == "show":
            if any(a.startswith("--property=Version") for a in argv):
                return _result(0 if self._manager_available else 1)
            return _result(0, self._show_map.get(argv[3] if len(argv) > 3 else "", ""))
        if verb in self._fail_verbs:
            return _result(1, stderr="redacted")
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls if len(c) > 2]


def _timer_show(
    *,
    active="active",
    enabled="enabled",
    next_monotonic="4w 1d 5h 2min 6.063752s",
    next_realtime="",
    last_trigger="Sun 2026-08-09 22:50:24 JST",
):
    """A timer ``show`` response shaped like a real MONOTONIC timer's.

    The defaults matter: systemd populates ``NextElapseUSecRealtime`` only for calendar timers, and
    this adapter's ``OnActiveSec`` / ``OnUnitActiveSec`` pair is monotonic — so a live timer leaves
    the realtime property EMPTY and answers in ``NextElapseUSecMonotonic``. The original fake echoed
    back whichever key the adapter happened to ask for, which is why reading the wrong property
    passed hermetically and only failed against a real user manager (Redmine #15183 smoke). These
    defaults reproduce the real shape, so a wrong property name fails here first.
    """
    return (
        f"ActiveState={active}\nUnitFileState={enabled}\n"
        f"NextElapseUSecRealtime={next_realtime}\n"
        f"NextElapseUSecMonotonic={next_monotonic}\n"
        f"LastTriggerUSec={last_trigger}\n"
    )


def _service_show(*, main_pid="0", result="success", status="0", exit_at="Sun 2026-08-09 22:30:35 JST"):
    return (
        f"MainPID={main_pid}\nResult={result}\n"
        f"ExecMainStatus={status}\nExecMainExitTimestamp={exit_at}\n"
    )


def _which_found(_name: str):
    return "/opt/bin/mozyo-bridge"


def _which_missing(_name: str):
    return None


def _which_relative(_name: str):
    # A relative PATH entry makes shutil.which return a relative path.
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

    def _runner(self, **kwargs) -> FakeRunner:
        """A runner whose owned timer reports active+enabled (the installed-and-scheduled case)."""
        show_map = kwargs.pop("show_map", None) or {
            ss.SUPERVISOR_UNIT.timer_unit: _timer_show(),
            ss.SUPERVISOR_UNIT.service_unit: _service_show(),
        }
        return FakeRunner(show_map=show_map, **kwargs)

    def _install(self, *, runner=None, interval=None, credential=True) -> dict:
        if credential:
            _write_home_credential(self.mozyo_home)
        kwargs = {} if interval is None else {"interval_seconds": interval}
        return ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=runner or self._runner(), which=_which_found, **kwargs,
        )


# ---------------------------------------------------------------------------
# Finding 1: ONE service + ONE timer. The retired dual-cadence shape must not come back.
# ---------------------------------------------------------------------------


class SingleOwnedUnitTest(_LinuxCase):
    def test_exactly_one_owned_service_and_one_owned_timer_exist(self) -> None:
        self.assertEqual(ss.SUPERVISOR_UNIT.service_unit, ss.SERVICE_UNIT_NAME)
        self.assertEqual(ss.SUPERVISOR_UNIT.timer_unit, ss.TIMER_UNIT_NAME)
        # No roster of units, and no second cadence hiding behind a different name.
        self.assertFalse(hasattr(ss, "SUPERVISOR_UNITS"))
        self.assertFalse(hasattr(ss, "DRAIN_UNIT"))
        self.assertFalse(hasattr(ss, "RECONCILE_UNIT"))

    def test_the_adapter_registers_no_drain_only_unit(self) -> None:
        # The retired requirement was registering BOTH --run-once and --drain-only with the OS.
        # Asserted against what the adapter actually EMITS and names, not its prose: the module
        # docstring legitimately explains the retired shape, and a guard that greps documentation
        # would fail on an accurate comment while still passing on a real regression.
        self.assertNotIn("--drain-only", ss.SUPERVISOR_UNIT.argv_tail)
        self.assertNotIn("drain", ss.SUPERVISOR_UNIT.service_unit)
        self.assertNotIn("drain", ss.SUPERVISOR_UNIT.timer_unit)
        rendered = ss.render_service_unit(
            ss.resolve_supervisor_command(mozyo_home=self.mozyo_home, which=_which_found)
        )
        self.assertNotIn("--drain-only", rendered)
        # No module-level name declares a second cadence.
        self.assertEqual([n for n in dir(ss) if "DRAIN" in n.upper()], [])

    def test_the_adapter_exposes_no_atomic_pair_install(self) -> None:
        # "install both at once, roll both back on failure" was removed from the contract.
        for retired in ("install_pair", "uninstall_pair", "restart_pair", "service_status_pair"):
            self.assertFalse(hasattr(ss, retired), retired)

    def test_installing_writes_exactly_two_files(self) -> None:
        self._install()
        written = sorted(p.name for p in ss.unit_dir(self.os_home).iterdir())
        self.assertEqual(written, sorted([ss.SERVICE_UNIT_NAME, ss.TIMER_UNIT_NAME]))

    def test_the_scheduled_command_is_run_once(self) -> None:
        self.assertEqual(ss.SUPERVISOR_UNIT.argv_tail, ("workflow", "supervisor", "--run-once"))


# ---------------------------------------------------------------------------
# Cadence: the shared portable OS tick, with the Redmine cadence left to the supervisor body.
# ---------------------------------------------------------------------------


class TickCadenceTest(_LinuxCase):
    def test_the_default_os_tick_is_the_shared_portable_default(self) -> None:
        # 180 is the measured portable default (#15192), and it is the SAME value the macOS adapter
        # registers at — one operator-facing cadence knob, not a per-OS number. The literal is
        # pinned here so a silent drift back to a private value fails.
        self.assertEqual(ss.DEFAULT_TICK_INTERVAL_SECONDS, DEFAULT_OS_TICK_INTERVAL_SECONDS)
        self.assertEqual(DEFAULT_OS_TICK_INTERVAL_SECONDS, 180)
        self.assertIn("OnUnitActiveSec=180s", ss.render_timer_unit())

    def test_the_tick_cadence_is_not_the_redmine_cadence(self) -> None:
        # The acceptance contract: tick on the OS cadence, but read Redmine on the existing ~300s
        # cadence. This adapter must not conflate them by scheduling the provider interval on the
        # timer, and the tick must stay strictly finer so a due watermark is never made to wait a
        # whole extra period for an aligned tick.
        self.assertNotEqual(ss.DEFAULT_TICK_INTERVAL_SECONDS, DEFAULT_RECONCILIATION_INTERVAL_SECONDS)
        self.assertLess(ss.DEFAULT_TICK_INTERVAL_SECONDS, DEFAULT_RECONCILIATION_INTERVAL_SECONDS)
        self.assertEqual(DEFAULT_RECONCILIATION_INTERVAL_SECONDS, 300)

    def test_the_adapter_does_not_set_or_enforce_the_provider_cadence(self) -> None:
        # Gating provider reads is the supervisor body's durable watermark, not this module's job.
        # Asserted structurally (the adapter neither imports nor calls the cadence policy) rather
        # than by grepping prose, which the docstring legitimately mentions.
        self.assertFalse(hasattr(ss, "should_reconcile_source"))
        self.assertFalse(hasattr(ss, "reconcile_backoff_seconds"))
        self.assertFalse(hasattr(ss, "ReconcileCadenceStore"))
        # ...and the provider interval never reaches a rendered timer.
        self.assertNotIn(
            f"OnUnitActiveSec={DEFAULT_RECONCILIATION_INTERVAL_SECONDS}s", ss.render_timer_unit()
        )

    def test_status_surfaces_the_provider_cadence_for_the_operator(self) -> None:
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._runner(), which=_which_found,
        )
        self.assertEqual(
            status["provider_reconcile_interval_seconds"], DEFAULT_RECONCILIATION_INTERVAL_SECONDS
        )

    def test_an_explicit_tick_interval_is_honoured(self) -> None:
        self._install(interval=90)
        self.assertIn(
            "OnUnitActiveSec=90s", ss.timer_unit_path(self.os_home).read_text(encoding="utf-8")
        )

    def test_a_non_positive_interval_is_floored_to_one_second(self) -> None:
        self.assertIn("OnUnitActiveSec=1s", ss.render_timer_unit(interval_seconds=0))
        self.assertIn("OnUnitActiveSec=1s", ss.render_timer_unit(interval_seconds=-5))


# ---------------------------------------------------------------------------
# Unit rendering: secret-free, no relaunch loop, one-shot.
# ---------------------------------------------------------------------------


class UnitRenderingTest(_LinuxCase):
    def _service_text(self) -> str:
        return ss.render_service_unit(
            ss.resolve_supervisor_command(mozyo_home=self.mozyo_home, which=_which_found)
        )

    def test_the_service_unit_is_a_one_shot_with_no_relaunch_directive(self) -> None:
        text = self._service_text()
        self.assertIn("Type=oneshot", text)
        # Structurally ABSENT, not set to a falsy value: either would be a tight relaunch loop.
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

    def test_the_execstart_is_the_exact_pinned_argv(self) -> None:
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

    def test_the_timer_pairs_run_at_load_with_the_fixed_interval(self) -> None:
        text = ss.render_timer_unit(interval_seconds=60)
        self.assertIn(f"OnActiveSec={ss.RUN_AT_LOAD_DELAY}", text)  # run one tick on activation
        self.assertIn("OnUnitActiveSec=60s", text)
        self.assertIn(f"Unit={ss.SERVICE_UNIT_NAME}", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_the_timer_declares_no_calendar_catch_up(self) -> None:
        # There is no missed run to replay: the next tick reconciles whatever the last one missed.
        text = ss.render_timer_unit()
        self.assertNotIn("OnCalendar=", text)
        self.assertNotIn("Persistent=", text)


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

    def test_a_literal_percent_is_escaped_so_systemd_does_not_expand_it(self) -> None:
        # Measured on a live user manager (review j#102053 F4): an unescaped `%h` in ExecStart is
        # expanded by systemd, so the executed argv was `/opt//home/holly/mozyo-bridge` while the
        # unit's literal text said `/opt/%h/mozyo-bridge`. Only `%%` pins a literal percent.
        rendered = ss.format_exec_argv(["/opt/%h/mozyo-bridge", "--home", "/tmp/%h"])
        self.assertIn("%%h", rendered)
        self.assertNotIn("/opt/%h", rendered)

    def test_a_percent_path_round_trips_through_the_escape(self) -> None:
        argv = ["/opt/%h/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", "/tmp/%t"]
        self.assertEqual(ss.parse_exec_argv(ss.format_exec_argv(argv)), argv)

    def test_a_lone_specifier_makes_the_readback_untrustworthy(self) -> None:
        # A hand-edited `%h` expands to a value only systemd knows, so the argv in the file is not
        # the argv that runs. Guessing it is literal would let a drifted command look like a match.
        self.assertIsNone(ss.parse_exec_argv('"/opt/%h/mozyo-bridge"'))
        self.assertIsNone(ss.parse_exec_argv('"/opt/bin/x" "--home" "/tmp/%t"'))

    def test_a_trailing_percent_is_also_untrustworthy(self) -> None:
        self.assertIsNone(ss.parse_exec_argv('"/opt/bin/100%"'))

    def test_an_escaped_percent_reads_back_as_one_literal_percent(self) -> None:
        self.assertEqual(ss.parse_exec_argv('"/opt/100%%/x"'), ["/opt/100%/x"])

    def test_a_control_character_cannot_be_pinned_and_is_reported(self) -> None:
        # A newline would not produce a weird path -- it would produce a DIFFERENT unit, because a
        # unit file is line-based and the tail would parse as another directive.
        for bad in ("/opt/a\nb", "/opt/a\rb", "/opt/a\x00b", "/opt/a\x1bb", "/opt/a\x7fb"):
            self.assertEqual(
                ss.unrenderable_argv_reason([bad]), ss.REASON_COMMAND_NOT_RENDERABLE, bad
            )

    def test_ordinary_and_awkward_but_renderable_paths_are_accepted(self) -> None:
        for ok in ("/opt/bin/mozyo-bridge", "/opt/my bin/x", '/opt/we"ird/x', "/opt/back\\slash",
                   "/opt/100%/x", "/opt/日本語/x"):
            self.assertEqual(ss.unrenderable_argv_reason([ok]), "", ok)


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

    def test_the_xdg_value_is_read_exactly_as_set(self) -> None:
        # Review j#102378 finding r7f3. The value was trimmed before the absolute test, which broke
        # the XDG Base Directory Specification in both directions at once: a value that is NOT
        # absolute (and which the spec says to treat as invalid and ignore) was promoted to a valid
        # root, and an absolute path naming a directory whose name ends in a space was redirected to
        # a different directory. This is not cosmetic — `install` writes the unit files into what
        # this returns and `uninstall` unlinks from it.
        cases = (
            (" /tmp/mozyo-xdg", Path.home() / ".config/systemd/user", "leading space: not absolute"),
            ("\t/tmp/mozyo-xdg", Path.home() / ".config/systemd/user", "leading tab: not absolute"),
            ("   ", Path.home() / ".config/systemd/user", "whitespace only: not absolute"),
            ("/tmp/mozyo-xdg ", Path("/tmp/mozyo-xdg /systemd/user"), "trailing space is part of it"),
            ("/tmp/mozyo-xdg", Path("/tmp/mozyo-xdg/systemd/user"), "the ordinary absolute case"),
        )
        for raw, expected, why in cases:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": raw}, clear=False):
                self.assertEqual(ss.unit_dir(), expected, why)

    def test_an_empty_or_unset_xdg_config_home_selects_the_default(self) -> None:
        # The spec's only two triggers for the default. Empty must not be confused with invalid:
        # both land on the default here, but for different documented reasons.
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}, clear=False):
            self.assertEqual(ss.unit_dir(), Path.home() / ".config/systemd/user")
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(ss.unit_dir(), Path.home() / ".config/systemd/user")

    def test_install_and_uninstall_touch_only_the_raw_authority_path(self) -> None:
        # The end-to-end consequence: a padded XDG value must not make install create units in — or
        # uninstall delete units from — a directory the raw value never named. `Path.home` is
        # redirected to a temp root so the ignored-value fallback stays inside the fixture.
        ignored = self.os_home / "cfg"
        fallback_home = self.os_home / "home"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": f" {ignored}"}, clear=False), \
                patch.object(Path, "home", return_value=fallback_home):
            result = ss.install(
                mozyo_home=self.mozyo_home, runner=self._runner(), which=_which_found
            )
            self.assertTrue(result["performed"], result)
            self.assertFalse(
                (ignored / "systemd/user").exists(),
                "a non-absolute XDG value must be ignored, not trimmed into a write target",
            )
            self.assertTrue((fallback_home / ".config/systemd/user" / ss.SERVICE_UNIT_NAME).exists())
            ss.uninstall(runner=self._runner())
            self.assertFalse(
                (fallback_home / ".config/systemd/user" / ss.SERVICE_UNIT_NAME).exists()
            )


# ---------------------------------------------------------------------------
# Finding 2: an unconfigured Redmine must NOT block installing the timer.
# ---------------------------------------------------------------------------


class CredentialIsProjectedNotGatedTest(_LinuxCase):
    def _assert_installed(self, result: dict, expected_readiness: str) -> None:
        self.assertTrue(result["performed"], result)
        self.assertEqual(result["credential_readiness"], expected_readiness)
        self.assertTrue(ss.service_unit_path(self.os_home).exists())
        self.assertTrue(ss.timer_unit_path(self.os_home).exists())

    def test_a_missing_credential_still_installs_the_timer(self) -> None:
        # The acceptance contract: local-only work must keep running when Redmine is unconfigured.
        result = self._install(credential=False)
        self._assert_installed(result, ss.CREDENTIAL_MISSING)

    def test_an_incomplete_credential_still_installs_the_timer(self) -> None:
        _write_home_credential(self.mozyo_home, url=None)
        result = self._install(credential=False)
        self._assert_installed(result, ss.CREDENTIAL_INCOMPLETE)

    def test_an_unsafe_credential_still_installs_the_timer(self) -> None:
        _write_home_credential(self.mozyo_home, mode=0o644)
        result = self._install(credential=False)
        self._assert_installed(result, ss.CREDENTIAL_UNSAFE)

    def test_an_unsafe_credential_is_never_read_into_the_unit(self) -> None:
        # The safety boundary is preserved by NOT using the value, not by refusing the install:
        # the resolver withholds an unsafe file's contents, so nothing can reach the unit.
        _write_home_credential(self.mozyo_home, api_key="super-secret-key", mode=0o644)
        self._install(credential=False)
        text = ss.service_unit_path(self.os_home).read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key", text)
        self.assertNotIn("redmine.example.test", text)

    def test_no_credential_refusal_token_exists_on_this_adapter(self) -> None:
        text = inspect.getsource(ss)
        self.assertNotIn("redmine_credential_missing", text)
        self.assertNotIn("redmine_credential_incomplete", text)
        self.assertNotIn("redmine_credential_unsafe", text)

    def test_a_shell_environment_can_never_make_readiness_ready(self) -> None:
        # A systemd unit carries no Environment block and inherits no interactive shell.
        with patch.dict(os.environ, SHELL_ENV, clear=False):
            result = self._install(credential=False)
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)
        self.assertTrue(result["performed"])  # ...and it still installs

    def test_a_shell_mozyo_bridge_home_can_never_redirect_the_readiness_root(self) -> None:
        other = Path(tempfile.mkdtemp())
        _write_home_credential(other)  # a READY credential in a DIFFERENT root
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(other)}, clear=False):
            result = self._install(credential=False)
        # The explicit --home root is what the unit pins, so ITS readiness is what gets reported.
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)


# ---------------------------------------------------------------------------
# Install: the refusals that remain are the ones that make the install meaningless.
# ---------------------------------------------------------------------------


class InstallRefusalTest(_LinuxCase):
    def _assert_zero_mutation(self, runner: FakeRunner) -> None:
        self.assertFalse(ss.unit_dir(self.os_home).exists())
        self.assertEqual([v for v in runner.verbs if v != "show"], [])

    def test_a_non_linux_host_refuses_before_any_mutation(self) -> None:
        runner = self._runner()
        with patch.object(sys, "platform", "darwin"):
            result = ss.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
            )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_UNSUPPORTED_PLATFORM)
        self._assert_zero_mutation(runner)

    def test_an_unreachable_user_manager_refuses_before_any_mutation(self) -> None:
        runner = self._runner(manager_available=False)
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
        runner = self._runner()
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_missing
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_EXECUTABLE_NOT_FOUND)
        self._assert_zero_mutation(runner)

    def test_an_unpinnable_executable_refuses_before_writing_a_corrupt_unit(self) -> None:
        # A newline in the resolved path cannot live on one unit-file line; writing it would emit a
        # different unit rather than an odd-looking one (review j#102053 F4).
        runner = self._runner()
        result = ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=runner, which=lambda _n: "/opt/bin/mozyo\nbridge",
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_COMMAND_NOT_RENDERABLE)
        self._assert_zero_mutation(runner)

    def test_an_unpinnable_mozyo_home_refuses_before_writing_a_corrupt_unit(self) -> None:
        bad_home = Path(self._tmp_mozyo.name) / "a\nb"
        runner = self._runner()
        result = ss.install(
            os_home=self.os_home, mozyo_home=bad_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_COMMAND_NOT_RENDERABLE)
        self._assert_zero_mutation(runner)

    def test_a_percent_home_installs_and_reads_back_literally(self) -> None:
        # End-to-end for the specifier boundary: install -> unit text -> status readback must all
        # agree on the literal path, and the status must still call it a match.
        percent_home = Path(self._tmp_mozyo.name) / "100%dir"
        percent_home.mkdir()
        ss.install(
            os_home=self.os_home, mozyo_home=percent_home,
            runner=self._runner(), which=_which_found,
        )
        text = ss.service_unit_path(self.os_home).read_text(encoding="utf-8")
        self.assertIn("%%dir", text)  # escaped in the file...
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        # ...and un-escaped back to the literal path on readback, still matching an install.
        self.assertEqual(status["installed_command"][-1], str(percent_home.resolve()))
        self.assertTrue(status["executable_matches"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_OK)


class InstallSuccessTest(_LinuxCase):
    def test_install_writes_both_units_then_reloads_and_enables_the_timer(self) -> None:
        runner = self._runner()
        result = self._install(runner=runner)
        self.assertTrue(result["performed"], result)
        self.assertEqual(result["scheduled_interval_seconds"], DEFAULT_OS_TICK_INTERVAL_SECONDS)
        self.assertEqual(
            [c for c in runner.calls if c[2] != "show"],
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", ss.TIMER_UNIT_NAME],
            ],
        )

    def test_install_is_idempotent(self) -> None:
        self._install()
        first = ss.service_unit_path(self.os_home).read_text(encoding="utf-8")
        self._install()
        self.assertEqual(ss.service_unit_path(self.os_home).read_text(encoding="utf-8"), first)

    def test_the_installed_unit_pins_the_absolute_canonical_mozyo_home(self) -> None:
        self._install()
        self.assertIn(
            str(self.mozyo_home.resolve()),
            ss.service_unit_path(self.os_home).read_text(encoding="utf-8"),
        )

    def test_a_relative_executable_is_pinned_as_an_absolute_path(self) -> None:
        # A relative PATH entry would be resolved from systemd's cwd, not the installer's.
        ss.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._runner(), which=_which_relative,
        )
        argv = ss.parse_exec_argv(
            [
                ln for ln in ss.service_unit_path(self.os_home).read_text().splitlines()
                if ln.startswith("ExecStart=")
            ][0][len("ExecStart="):]
        )
        self.assertTrue(os.path.isabs(argv[0]), argv)

    def test_a_failed_daemon_reload_reports_a_redacted_token(self) -> None:
        runner = self._runner(fail_verbs=("daemon-reload",))
        result = self._install(runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_DAEMON_RELOAD_FAILED)
        self.assertNotIn("enable", runner.verbs)

    def test_a_failed_enable_reports_a_redacted_token(self) -> None:
        runner = self._runner(fail_verbs=("enable",))
        result = self._install(runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_ENABLE_FAILED)


# ---------------------------------------------------------------------------
# Restart: the installed unit is the authority; never re-run a drifted command.
# ---------------------------------------------------------------------------


class RestartTest(_LinuxCase):
    def test_restart_runs_the_service_when_the_owned_timer_is_active(self) -> None:
        self._install()
        runner = self._runner()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(result["performed"], result)
        self.assertIn(["systemctl", "--user", "restart", ss.SERVICE_UNIT_NAME], runner.calls)

    def test_restart_works_without_a_configured_credential(self) -> None:
        # Same reason install does: a non-ready Redmine must not stop local-safe work.
        self._install(credential=False)
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._runner(), which=_which_found,
        )
        self.assertTrue(result["performed"], result)
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)

    def test_restart_refuses_when_nothing_is_installed(self) -> None:
        runner = self._runner()
        result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_NOT_INSTALLED)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_when_the_owned_timer_is_not_active(self) -> None:
        self._install()
        runner = self._runner(
            show_map={ss.SUPERVISOR_UNIT.timer_unit: _timer_show(active="inactive")}
        )
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertEqual(result["reason"], ss.REASON_SERVICE_NOT_LOADED)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_and_status_classify_one_manager_answer_the_same_way(self) -> None:
        # Review j#102327 finding r6f2. `restart` compared the raw value to `active` while `status`
        # ran the same reply through the closed vocabulary, so one `ActiveState=reloading` timer was
        # `loaded` to status and `service_not_loaded` to restart — two answers about one state
        # machine. Both verbs now read the same classification.
        self._install()
        for active in ("active", "reloading", "inactive", "failed", "activating",
                       "deactivating", "maintenance", "ACTIVE", "RELOADING", "INACTIVE", "",
                       " active ", "active ", "\tinactive", "  failed  "):
            show_map = {ss.SUPERVISOR_UNIT.timer_unit: _timer_show(active=active)}
            status = ss.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=self._runner(show_map=show_map), which=_which_found,
            )
            runner = self._runner(show_map=show_map)
            result = ss.restart(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=runner, which=_which_found,
            )
            self.assertEqual(result["performed"], status["loaded"], active)
            self.assertEqual("restart" in runner.verbs, status["loaded"], active)
            if not status["loaded"]:
                # The r6f2 invariant is that both verbs read ONE classification — which now also
                # means the refusal names which fact refused: a confirmed stop and an unreadable
                # state are different answers (review j#102383 finding r8f2), and status already
                # distinguishes them via `probe_state`.
                expected = (
                    ss.REASON_SERVICE_NOT_LOADED
                    if status["probe_state"] == ss.PROBE_CONFIRMED_ABSENT
                    else ss.REASON_TIMER_STATE_UNREADABLE
                )
                self.assertEqual(result["reason"], expected, active)

    def test_restart_refuses_an_unreadable_service_unit(self) -> None:
        self._install()
        ss.service_unit_path(self.os_home).write_text("not a unit at all\n", encoding="utf-8")
        runner = self._runner()
        result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_UNHEALTHY)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_UNREADABLE)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_a_unit_with_two_execstart_lines(self) -> None:
        self._install()
        target = ss.service_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8") + 'ExecStart="/opt/bin/other"\n', encoding="utf-8"
        )
        result = ss.restart(os_home=self.os_home, runner=self._runner(), which=_which_found)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_UNREADABLE)

    def test_restart_refuses_a_missing_home_pin(self) -> None:
        self._install()
        ss.service_unit_path(self.os_home).write_text(
            ss.render_service_unit(["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"]),
            encoding="utf-8",
        )
        result = ss.restart(os_home=self.os_home, runner=self._runner(), which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_UNHEALTHY)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_MISSING)

    def test_restart_refuses_a_relative_home_pin(self) -> None:
        self._install()
        ss.service_unit_path(self.os_home).write_text(
            ss.render_service_unit(
                ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", "rel/root"]
            ),
            encoding="utf-8",
        )
        result = ss.restart(os_home=self.os_home, runner=self._runner(), which=_which_found)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_NOT_ABSOLUTE)

    def test_restart_refuses_a_requested_home_that_disagrees_with_the_pin(self) -> None:
        self._install()
        runner = self._runner()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=Path(tempfile.mkdtemp()),
            runner=runner, which=_which_found,
        )
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_MISMATCH)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_refuses_a_hand_edited_specifier_instead_of_trusting_it(self) -> None:
        # A `%h` in the installed unit means systemd runs a path we cannot reproduce, so the
        # readback is untrustworthy and restart must fail closed rather than compare literals.
        self._install()
        ss.service_unit_path(self.os_home).write_text(
            "[Unit]\nDescription=x\n\n[Service]\nType=oneshot\n"
            'ExecStart="/opt/%h/mozyo-bridge" "workflow" "supervisor" "--run-once" '
            '"--home" "/tmp/x"\n',
            encoding="utf-8",
        )
        runner = self._runner()
        result = ss.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(result["reason"], ss.REASON_HOME_PIN_UNHEALTHY)
        self.assertEqual(result["home_pin"], ss.HOME_PIN_UNREADABLE)
        self.assertNotIn("restart", runner.verbs)

    def test_status_reports_a_hand_edited_specifier_as_not_matching(self) -> None:
        self._install()
        ss.service_unit_path(self.os_home).write_text(
            "[Unit]\nDescription=x\n\n[Service]\nType=oneshot\n"
            'ExecStart="/opt/%h/mozyo-bridge" "--home" "/tmp/x"\n',
            encoding="utf-8",
        )
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertFalse(status["executable_matches"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_UNREADABLE)

    def test_restart_refuses_a_drifted_installed_command(self) -> None:
        self._install()
        runner = self._runner()
        result = ss.restart(
            os_home=self.os_home, runner=runner, which=lambda _n: "/somewhere/else/mozyo-bridge"
        )
        self.assertEqual(result["reason"], ss.REASON_INSTALLED_COMMAND_DRIFT)
        self.assertNotIn("restart", runner.verbs)

    def test_restart_reads_readiness_from_the_pinned_home_not_the_caller_shell(self) -> None:
        self._install()
        credentials_path(self.mozyo_home).unlink()  # the PINNED root is no longer ready
        ready_elsewhere = Path(tempfile.mkdtemp())
        _write_home_credential(ready_elsewhere)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(ready_elsewhere)}, clear=False):
            result = ss.restart(os_home=self.os_home, runner=self._runner(), which=_which_found)
        self.assertEqual(result["credential_readiness"], ss.CREDENTIAL_MISSING)

    def test_a_failed_restart_reports_a_redacted_token(self) -> None:
        self._install()
        runner = self._runner(fail_verbs=("restart",))
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_RESTART_FAILED)


# ---------------------------------------------------------------------------
# Uninstall: no credential required; removes exactly the owned artifacts, files AND manager state.
# ---------------------------------------------------------------------------


class UninstallTest(_LinuxCase):
    def test_uninstall_disables_stops_removes_and_clears_manager_state(self) -> None:
        self._install()
        foreign = ss.unit_dir(self.os_home) / "someone-elses.service"
        foreign.write_text("[Unit]\n", encoding="utf-8")
        runner = self._runner()
        result = ss.uninstall(os_home=self.os_home, runner=runner)
        self.assertTrue(result["performed"])
        self.assertTrue(result["removed"])
        self.assertFalse(ss.service_unit_path(self.os_home).exists())
        self.assertFalse(ss.timer_unit_path(self.os_home).exists())
        self.assertTrue(foreign.exists())  # untouched
        self.assertEqual(
            [c for c in runner.calls if c[2] != "show"],
            [
                ["systemctl", "--user", "disable", "--now", ss.TIMER_UNIT_NAME],
                ["systemctl", "--user", "stop", ss.SERVICE_UNIT_NAME],
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "reset-failed", ss.SERVICE_UNIT_NAME, ss.TIMER_UNIT_NAME],
            ],
        )

    def test_the_manager_state_is_cleared_after_the_files_are_gone(self) -> None:
        # Measured live (#15183 smoke): ``stop`` on a mid-flight sweep SIGTERMs it, so systemd keeps
        # a ``failed`` record that survives file removal as a ``not-found``/``failed`` list-units
        # entry. Ordering matters: a reset-failed BEFORE daemon-reload would be re-dirtied.
        self._install()
        runner = self._runner()
        ss.uninstall(os_home=self.os_home, runner=runner)
        verbs = [c[2] for c in runner.calls if c[2] != "show"]
        self.assertLess(verbs.index("daemon-reload"), verbs.index("reset-failed"))

    def test_uninstall_works_with_no_credential_at_all(self) -> None:
        result = ss.uninstall(os_home=self.os_home, runner=self._runner())
        self.assertTrue(result["performed"])
        self.assertFalse(result["removed"])

    def test_uninstall_refuses_on_a_non_linux_host(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            result = ss.uninstall(os_home=self.os_home, runner=self._runner())
        self.assertEqual(result["reason"], ss.REASON_UNSUPPORTED_PLATFORM)


# ---------------------------------------------------------------------------
# Finding 3: status must show next run, last exit result, and the installed command.
# ---------------------------------------------------------------------------


class ShowDuplicatePropertyTest(_LinuxCase):
    """Review j#102383 finding r8f2: a reply that answers one property twice, differently.

    ``_show`` assigned into a dict, so the last line silently won. The same contradictory reply then
    produced OPPOSITE confirmed facts depending on line order — and the winning value went on to
    authorize a real ``systemctl restart``. Nothing makes either line authoritative, so the read is
    discarded rather than resolved.
    """

    def _shown(self, stdout: str) -> dict:
        return ss._show(lambda _cmd: _result(0, stdout), "x.timer", ("ActiveState",))

    def test_conflicting_duplicates_discard_the_read_in_either_order(self) -> None:
        for stdout, why in (
            ("ActiveState=inactive\nActiveState=active\n", "absent then loaded"),
            ("ActiveState=active\nActiveState=inactive\n", "loaded then absent"),
        ):
            self.assertEqual(self._shown(stdout), {}, why)
            self.assertEqual(
                ss._probe_state(self._shown(stdout), manager_available=True),
                ss.PROBE_UNREADABLE,
                why,
            )

    def test_a_conflict_on_any_requested_property_discards_the_read(self) -> None:
        # Not just the one the classifier happens to read: an unresolvable reply is unresolvable.
        stdout = "UnitFileState=enabled\nActiveState=active\nUnitFileState=disabled\n"
        self.assertEqual(self._shown(stdout), {})

    def test_a_repeated_property_with_the_SAME_value_is_not_a_conflict(self) -> None:
        # Stated and pinned deliberately: the rule is about contradiction, not repetition. Refusing
        # identical repeats would be an over-refusal with nothing behind it.
        self.assertEqual(
            self._shown("ActiveState=active\nActiveState=active\n"), {"ActiveState": "active"}
        )

    def test_a_single_answer_and_a_missing_key_are_unchanged(self) -> None:
        self.assertEqual(self._shown("ActiveState=active\n"), {"ActiveState": "active"})
        self.assertEqual(self._shown("SomethingElse=x\n"), {"SomethingElse": "x"})
        self.assertEqual(
            ss._probe_state(self._shown("SomethingElse=x\n"), manager_available=True),
            ss.PROBE_UNREADABLE,
        )

    def test_restart_makes_zero_mutation_on_a_conflicting_reply(self) -> None:
        # The consumer the finding turned on: the last-wins value reached `systemctl restart`.
        runner = self._runner(
            show_map={
                ss.TIMER_UNIT_NAME: "ActiveState=inactive\nActiveState=active\n",
                ss.SERVICE_UNIT_NAME: _service_show(),
            }
        )
        self._install(runner=runner)
        runner.calls.clear()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertNotIn("restart", runner.verbs)
        # And it says WHICH fact refused: unreadable is not "the timer is stopped".
        self.assertEqual(result["reason"], ss.REASON_TIMER_STATE_UNREADABLE)
        self.assertEqual(result["probe_state"], ss.PROBE_UNREADABLE)

    def test_restart_still_refuses_a_genuinely_stopped_timer_with_its_own_reason(self) -> None:
        runner = self._runner(
            show_map={
                ss.TIMER_UNIT_NAME: "ActiveState=inactive\n",
                ss.SERVICE_UNIT_NAME: _service_show(),
            }
        )
        self._install(runner=runner)
        runner.calls.clear()
        result = ss.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], ss.REASON_SERVICE_NOT_LOADED)
        self.assertNotIn("restart", runner.verbs)


class ServiceStatusTest(_LinuxCase):
    def test_status_reports_the_next_scheduled_run_of_a_monotonic_timer(self) -> None:
        # The real shape: this adapter's timer is monotonic, so systemd leaves the REALTIME property
        # empty. Reading only that one reported a blank "next run" against a live scheduled timer
        # (Redmine #15183 smoke); the projection must fall through to the monotonic property.
        self._install()
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertEqual(status["next_elapse"], "4w 1d 5h 2min 6.063752s")
        self.assertEqual(status["next_elapse_basis"], ss.NEXT_ELAPSE_MONOTONIC)
        self.assertNotEqual(status["next_elapse"], "")

    def test_a_calendar_timer_next_run_is_reported_as_realtime(self) -> None:
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(
                    next_realtime="Sun 2026-08-09 22:31:05 JST", next_monotonic=""
                ),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(),
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(status["next_elapse"], "Sun 2026-08-09 22:31:05 JST")
        self.assertEqual(status["next_elapse_basis"], ss.NEXT_ELAPSE_REALTIME)

    def test_the_next_elapse_basis_is_empty_when_systemd_reports_neither(self) -> None:
        # A basis is required to read the value: a monotonic figure is since-boot, not a wall clock,
        # so an unlabelled value could be misread as a wall-clock time.
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(next_realtime="", next_monotonic=""),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(),
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(status["next_elapse"], "")
        self.assertEqual(status["next_elapse_basis"], ss.NEXT_ELAPSE_UNKNOWN)

    def test_status_asks_systemd_for_both_next_elapse_properties(self) -> None:
        # Pins the exact property names. A rename or a drop to one property reintroduces the
        # blank-next-run defect, and this fails before a live host ever sees it.
        self._install()
        runner = self._runner()
        ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        asked = {
            arg[len("--property="):]
            for call in runner.calls if call[2] == "show"
            for arg in call if arg.startswith("--property=")
        }
        self.assertIn("NextElapseUSecRealtime", asked)
        self.assertIn("NextElapseUSecMonotonic", asked)
        self.assertIn("LastTriggerUSec", asked)

    def test_status_reports_the_last_trigger_wall_clock(self) -> None:
        self._install()
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertEqual(status["last_trigger"], "Sun 2026-08-09 22:50:24 JST")

    def test_status_reports_the_last_exit_result(self) -> None:
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(
                    result="exit-code", status="2", exit_at="Sun 2026-08-09 22:30:35 JST"
                ),
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(status["last_result"], "exit-code")
        self.assertEqual(status["last_exit_status"], 2)
        self.assertEqual(status["last_exit_at"], "Sun 2026-08-09 22:30:35 JST")

    def test_status_reports_a_signal_killed_last_run(self) -> None:
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(result="signal", status="-1"),
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual(status["last_result"], "signal")
        self.assertEqual(status["last_exit_status"], -1)

    def test_status_reports_the_installed_command(self) -> None:
        self._install()
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertEqual(
            status["installed_command"],
            [
                "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
                "--home", str(self.mozyo_home.resolve()),
            ],
        )

    def test_status_projects_the_installed_and_scheduled_service(self) -> None:
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(main_pid="4321"),
            }
        )
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertTrue(status["timer_enabled"])
        self.assertEqual(status["pid"], 4321)
        self.assertEqual(status["scheduled_interval_seconds"], DEFAULT_OS_TICK_INTERVAL_SECONDS)
        self.assertTrue(status["run_at_load"])
        self.assertFalse(status["keep_alive_present"])
        self.assertTrue(status["no_environment_block"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_OK)
        self.assertTrue(status["executable_matches"])
        self.assertEqual(status["credential_readiness"], ss.CREDENTIAL_READY)

    def test_status_mutates_nothing(self) -> None:
        runner = self._runner()
        ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertEqual([v for v in runner.verbs if v != "show"], [])
        self.assertFalse(ss.unit_dir(self.os_home).exists())

    def test_status_emits_no_secret(self) -> None:
        _write_home_credential(self.mozyo_home, api_key="super-secret-key")
        self._install(credential=False)
        blob = repr(
            ss.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=self._runner(), which=_which_found,
            )
        )
        self.assertNotIn("super-secret-key", blob)
        self.assertNotIn("redmine.example.test", blob)

    def test_an_uninstalled_host_reports_the_would_be_root_readiness(self) -> None:
        status = ss.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=self._runner(), which=_which_found,
        )
        self.assertFalse(status["installed"])
        self.assertEqual(status["home_pin"], ss.HOME_PIN_NOT_INSTALLED)
        self.assertEqual(status["credential_readiness"], ss.CREDENTIAL_MISSING)
        self.assertEqual(
            status["scheduled_interval_seconds"], DEFAULT_OS_TICK_INTERVAL_SECONDS
        )  # the hint
        self.assertEqual(status["installed_command"], [])

    def test_a_present_but_unreadable_unit_is_distinct_from_absence(self) -> None:
        self._install()
        ss.service_unit_path(self.os_home).write_text("garbage\n", encoding="utf-8")
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertEqual(status["home_pin"], ss.HOME_PIN_UNREADABLE)
        self.assertEqual(status["credential_readiness"], "")  # unknowable, never guessed
        self.assertFalse(status["executable_matches"])

    def test_a_lone_service_file_is_not_reported_as_installed(self) -> None:
        self._install()
        ss.timer_unit_path(self.os_home).unlink()  # no cadence left
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertFalse(status["installed"])
        self.assertTrue(status["service_unit_exists"])
        self.assertFalse(status["timer_unit_exists"])

    def test_a_hand_added_restart_directive_surfaces_as_keep_alive_present(self) -> None:
        self._install()
        target = ss.service_unit_path(self.os_home)
        target.write_text(target.read_text(encoding="utf-8") + "Restart=always\n", encoding="utf-8")
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertTrue(status["keep_alive_present"])

    def test_a_hand_added_environment_key_surfaces_as_an_environment_block(self) -> None:
        self._install()
        target = ss.service_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8") + "Environment=FOO=bar\n", encoding="utf-8"
        )
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertFalse(status["no_environment_block"])

    def test_an_unparseable_cadence_is_reported_as_unknown_not_reinterpreted(self) -> None:
        self._install()
        target = ss.timer_unit_path(self.os_home)
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                f"OnUnitActiveSec={DEFAULT_OS_TICK_INTERVAL_SECONDS}s", "OnUnitActiveSec=5min"
            ),
            encoding="utf-8",
        )
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertIsNone(status["scheduled_interval_seconds"])

    def test_a_non_ascii_pid_reads_as_none_instead_of_raising(self) -> None:
        # The Redmine #14753 defect class: ``str.isdigit()`` accepts characters that are not pids.
        self._install()
        runner = self._runner(
            show_map={
                ss.SUPERVISOR_UNIT.timer_unit: _timer_show(),
                ss.SUPERVISOR_UNIT.service_unit: _service_show(main_pid="²", status="²"),
            }
        )
        status = ss.service_status(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertIsNone(status["pid"])
        self.assertIsNone(status["last_exit_status"])

    def test_a_zero_main_pid_reads_as_no_running_sweep(self) -> None:
        self._install()
        status = ss.service_status(
            os_home=self.os_home, runner=self._runner(), which=_which_found
        )
        self.assertIsNone(status["pid"])


# ---------------------------------------------------------------------------
# Backend selection: one operator contract over two deliberately different host shapes.
# ---------------------------------------------------------------------------


class BackendSelectionTest(unittest.TestCase):
    def test_each_platform_resolves_to_its_owned_adapter(self) -> None:
        self.assertEqual(sb.resolve_backend_name("darwin"), sb.BACKEND_LAUNCHD)
        self.assertEqual(sb.resolve_backend_name("linux"), sb.BACKEND_SYSTEMD)
        self.assertEqual(sb.resolve_backend_name("linux2"), sb.BACKEND_SYSTEMD)
        self.assertEqual(sb.resolve_backend_name("win32"), sb.BACKEND_UNSUPPORTED)

    def test_the_resolved_modules_are_the_two_adapters(self) -> None:
        self.assertEqual(
            sb.resolve_backend("linux")[1].__name__.rsplit(".", 1)[-1], "supervisor_systemd"
        )
        self.assertEqual(
            sb.resolve_backend("darwin")[1].__name__.rsplit(".", 1)[-1], "supervisor_launchd"
        )
        self.assertIsNone(sb.resolve_backend("win32")[1])

    def test_both_adapters_expose_the_same_single_service_verbs(self) -> None:
        # #15192 retired the macOS pair, so there is no per-backend call shape left: the dispatcher
        # can call one set of verb names on either adapter. A pair verb coming back would silently
        # reintroduce a second registration on one host only.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        for verb in ("install", "restart", "uninstall", "service_status"):
            self.assertTrue(hasattr(supervisor_launchd, verb), verb)
            self.assertTrue(hasattr(ss, verb), verb)
        for retired in ("install_pair", "restart_pair", "uninstall_pair", "service_status_pair"):
            self.assertFalse(hasattr(supervisor_launchd, retired), retired)
        self.assertEqual(len(supervisor_launchd.SUPERVISOR_AGENTS), 1)

    def test_a_linux_result_is_normalized_to_a_single_row_roster(self) -> None:
        def fake_install(**kwargs):
            return {"action": "install", "performed": True, "reason": "", "label": "L"}

        with patch.object(sys, "platform", "linux"), patch.object(ss, "install", fake_install):
            result = sb.install(mozyo_home=Path("/tmp/x"))
        self.assertEqual(result["backend"], sb.BACKEND_SYSTEMD)
        self.assertEqual(len(result["agents"]), 1)
        self.assertTrue(result["performed"])

    def test_a_macos_result_is_normalized_to_a_single_row_roster(self) -> None:
        def fake_install(**kwargs):
            return {"action": "install", "performed": True, "reason": "", "label": "L"}

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        with patch.object(sys, "platform", "darwin"), patch.object(
            supervisor_launchd, "install", fake_install
        ):
            result = sb.install()
        self.assertEqual(result["backend"], sb.BACKEND_LAUNCHD)
        self.assertEqual(len(result["agents"]), 1)

    def test_the_tick_interval_reaches_both_adapters(self) -> None:
        # One cadence knob, one meaning: `--tick-interval` sets the single owned registration's
        # interval on either host. It used to be silently dropped on macOS (#15192).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        for platform, adapter in (("linux", ss), ("darwin", supervisor_launchd)):
            seen = {}

            def fake_install(**kwargs):
                seen.update(kwargs)
                return {"action": "install", "performed": True, "reason": ""}

            with patch.object(sys, "platform", platform), patch.object(
                adapter, "install", fake_install
            ):
                sb.install(interval_seconds=90)
            self.assertEqual(seen["interval_seconds"], 90, platform)

    def test_an_unsupported_host_gets_a_typed_zero_mutation_refusal(self) -> None:
        with patch.object(sys, "platform", "win32"):
            for verb in (sb.install, sb.restart, sb.uninstall):
                result = verb()
                self.assertFalse(result["performed"], verb)
                self.assertEqual(result["reason"], sb.REASON_NO_BACKEND, verb)
                self.assertEqual(result["agents"], [], verb)
            status = sb.service_status()
        self.assertEqual(status["backend"], sb.BACKEND_UNSUPPORTED)
        self.assertFalse(status["platform_supported"])


class NoResidentDaemonTest(unittest.TestCase):
    """The adapter must never introduce a resident daemon / infinite poll (issue #15183 scope)."""

    def test_the_adapter_declares_no_always_on_directive(self) -> None:
        text = inspect.getsource(ss)
        for directive in ("Restart=always", "Restart=on-failure", "RemainAfterExit=yes"):
            self.assertNotIn(directive, text)

    def test_the_adapter_declares_no_hibernate_cadence(self) -> None:
        self.assertNotIn("--hibernate", inspect.getsource(ss))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
