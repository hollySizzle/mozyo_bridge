"""macOS LaunchAgent lifecycle tests for the callback supervisor (Redmine #13683 Phase B1).

Real launchctl is never invoked and the real host LaunchAgents dir is never touched: every
subprocess call goes through an injected fake runner, and temp roots stand in for the OS user home
(``os_home``: plist/log) and the mozyo home (``mozyo_home``: credential/registry root) — two roots
that are kept **distinct** (review j#79092 R2-F1). These pin the Phase B1 safety boundary —

- plist structure: no ``EnvironmentVariables`` key, ``RunAtLoad`` + ``StartInterval``, **no**
  ``KeepAlive``, exact PATH-resolved executable argv with the resolved mozyo home pinned as ``--home``;
- structured launchctl argv (bootout-then-bootstrap install, kickstart -k restart, exact-file
  uninstall), idempotent install;
- fail-closed **zero-mutation** refusals: non-darwin host, missing executable, and the Redmine
  credential matrix — daemon-effective readiness (neither shell key/URL (j#79059 F1) nor a shell
  ``MOZYO_BRIDGE_HOME`` (j#79092 R2-F1) can make it ``ready``);
- the install preflight and the launchd daemon resolve the **same** absolute mozyo home, and
  restart / status take the installed plist's ``--home`` pin as the authority — never the caller's
  current shell (j#79125 R3-F1) — with an explicit mozyo home normalized to an absolute canonical
  root (j#79125 R3-F2);
- a redacted status projection (booleans / counts / fixed tokens; no secret, no path);

without touching the host. Live launchd operation is a separate coordinator gate (never here).
"""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_launchd as sl,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    default_redmine_source,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    API_KEY_ENV,
    BASE_URL_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    credentials_path,
)

#: A shell env that WOULD look ready on the interactive path — but the launchd daemon never sees it.
SHELL_ENV = {API_KEY_ENV: "shell-key-sentinel", BASE_URL_ENV: "https://redmine.shell.test"}

#: The uid the tests pin. It must be the REAL process uid: the `os.getuid` patch in
#: `_DarwinCase` is visible process-wide (shared `os` module), so the credential
#: ownership check in redmine_credentials compares fixture files owned by the actual
#: runner uid against this value. A fixed 501 only passed where the operator uid
#: happened to be 501 and broke on Linux CI runners.
_TEST_UID = os.getuid() if hasattr(os, "getuid") else 501
_GUI_DOMAIN = f"gui/{_TEST_UID}"


def _resolved(p: Path) -> str:
    """The absolute canonical string a ``--home`` pin uses for ``p`` (matches resolve_mozyo_home)."""
    return str(sl.resolve_mozyo_home(p))


def _write_home_credential(mozyo_home: Path, *, api_key="home-key-sentinel", url="https://redmine.example.test",
                           mode=0o600) -> Path:
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


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def _pinned_plist(os_home: Path, home: str, *, executable="/opt/bin/mozyo-bridge",
                  extra=()) -> Path:
    """Write an owned plist whose ProgramArguments pin ``home`` (test double for an install)."""
    argv = [executable, "workflow", "supervisor", "--run-once", "--home", home, *extra]
    target = sl.plist_path(os_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(sl.render_plist(argv, interval_seconds=300, os_home=os_home))
    return target


class FakeRunner:
    """Records every structured argv and returns a scripted (or default-ok) result."""

    def __init__(self, *, print_result=None, default=None) -> None:
        self.calls: list[list[str]] = []
        self._print_result = print_result
        self._default = default or _result(0)

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if len(argv) >= 2 and argv[1] == "print" and self._print_result is not None:
            return self._print_result
        return self._default

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) >= 2]


def _which_found(_name: str):
    return "/opt/bin/mozyo-bridge"


def _which_missing(_name: str):
    return None


def _which_relative(_name: str):
    # A relative PATH entry makes shutil.which return a relative path (R5-F1).
    return "bin/mozyo-bridge"


class _DarwinCase(unittest.TestCase):
    """Base: force darwin + a fixed uid, and provide distinct os_home / mozyo_home temp roots."""

    def setUp(self) -> None:
        self._tmp_os = tempfile.TemporaryDirectory()
        self._tmp_mozyo = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_os.cleanup)
        self.addCleanup(self._tmp_mozyo.cleanup)
        self.os_home = Path(self._tmp_os.name)
        self.mozyo_home = Path(self._tmp_mozyo.name)
        p_darwin = patch.object(sl, "_running_on_darwin", return_value=True)
        p_uid = patch.object(sl.os, "getuid", return_value=_TEST_UID, create=True)
        p_darwin.start()
        p_uid.start()
        self.addCleanup(p_darwin.stop)
        self.addCleanup(p_uid.stop)


class RenderPlistTest(unittest.TestCase):
    def test_plist_is_one_shot_scheduled_and_carries_no_environment_or_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = sl.render_plist(
                ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", "/x"],
                interval_seconds=300,
                os_home=Path(tmp),
            )
        payload = plistlib.loads(raw)
        self.assertEqual(sl.SUPERVISOR_LAUNCHD_LABEL, payload["Label"])
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", "/x"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(300, payload["StartInterval"])
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("EnvironmentVariables", payload)
        text = raw.decode("utf-8")
        for token in ("API_KEY", "REDMINE", "TOKEN", "SECRET"):
            self.assertNotIn(token, text)

    def test_interval_is_clamped_to_at_least_one(self) -> None:
        payload = plistlib.loads(sl.render_plist(["/opt/bin/mozyo-bridge"], interval_seconds=0))
        self.assertEqual(1, payload["StartInterval"])

    def test_secret_in_daemon_env_never_serializes_into_plist(self) -> None:
        with patch.dict("os.environ", {API_KEY_ENV: "SECRET-KEY-SENTINEL"}, clear=False):
            raw = sl.render_plist(["/opt/bin/mozyo-bridge"], interval_seconds=300)
        self.assertNotIn(b"SECRET-KEY-SENTINEL", raw)


class ResolveHomeAndCommandTest(unittest.TestCase):
    def test_explicit_relative_home_is_normalized_to_absolute(self) -> None:
        # R3-F2: a relative / tilde input must never be pinned as-is.
        self.assertTrue(sl.resolve_mozyo_home(Path("relative-home")).is_absolute())
        self.assertTrue(sl.resolve_mozyo_home(Path("~/some-home")).is_absolute())

    def test_command_pins_absolute_home_for_relative_input(self) -> None:
        cmd = sl.resolve_supervisor_command(mozyo_home=Path("relative-home"), which=_which_found)
        self.assertEqual(cmd[:5], ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home"])
        self.assertTrue(Path(cmd[5]).is_absolute())

    def test_command_pins_resolved_mozyo_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = sl.resolve_supervisor_command(mozyo_home=Path(tmp), which=_which_found)
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", _resolved(Path(tmp))],
            cmd,
        )

    def test_relative_executable_is_normalized_to_absolute(self) -> None:
        # R5-F1: a relative which() result must never be pinned as-is.
        cmd = sl.resolve_supervisor_command(mozyo_home=Path("/tmp"), which=_which_relative)
        self.assertTrue(Path(cmd[0]).is_absolute())
        self.assertEqual(os.path.abspath("bin/mozyo-bridge"), cmd[0])

    def test_missing_executable_is_none_not_a_shell_string(self) -> None:
        self.assertIsNone(sl.resolve_supervisor_command(which=_which_missing))


class ExtractPinnedHomeTest(unittest.TestCase):
    def test_ok_single_pin(self) -> None:
        argv = ["/x", "workflow", "supervisor", "--run-once", "--home", "/root"]
        self.assertEqual(("/root", sl.HOME_PIN_OK), sl._extract_pinned_home(argv))

    def test_missing_pin(self) -> None:
        self.assertEqual((None, sl.HOME_PIN_MISSING), sl._extract_pinned_home(["/x", "--run-once"]))

    def test_duplicate_pin(self) -> None:
        argv = ["/x", "--home", "/a", "--home", "/b"]
        self.assertEqual((None, sl.HOME_PIN_DUPLICATE), sl._extract_pinned_home(argv))

    def test_malformed_pin_value_missing_or_flaglike(self) -> None:
        self.assertEqual((None, sl.HOME_PIN_MALFORMED), sl._extract_pinned_home(["/x", "--home"]))
        self.assertEqual((None, sl.HOME_PIN_MALFORMED), sl._extract_pinned_home(["/x", "--home", "--json"]))

    def test_relative_or_noncanonical_pin_is_not_absolute(self) -> None:
        # R4-F1: only an absolute, lexically-canonical path is trusted.
        for bad in ("relative-home", "~/mozyo", "/a/../b", "/a/./b", "/a//b"):
            self.assertEqual(
                (None, sl.HOME_PIN_NOT_ABSOLUTE),
                sl._extract_pinned_home(["/x", "--home", bad]),
                bad,
            )

    def test_no_argv(self) -> None:
        self.assertEqual((None, sl.HOME_PIN_NO_ARGV), sl._extract_pinned_home(None))


class CredentialReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mozyo_home = Path(self._tmp.name)

    def test_ready_only_from_secure_home_credential_file(self) -> None:
        _write_home_credential(self.mozyo_home)
        self.assertEqual(sl.CREDENTIAL_READY, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))

    def test_env_only_is_not_ready_daemon_never_sees_shell_env(self) -> None:
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            self.assertEqual(
                sl.CREDENTIAL_MISSING, sl.classify_credential_readiness(mozyo_home=self.mozyo_home)
            )

    def test_shell_mozyo_home_override_does_not_leak_into_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            _write_home_credential(Path(other))
            with patch.dict("os.environ", {"MOZYO_BRIDGE_HOME": other}, clear=False):
                self.assertEqual(
                    sl.CREDENTIAL_MISSING,
                    sl.classify_credential_readiness(mozyo_home=self.mozyo_home),
                )

    def test_incomplete_when_home_file_has_only_key(self) -> None:
        _write_home_credential(self.mozyo_home, url=None)
        self.assertEqual(sl.CREDENTIAL_INCOMPLETE, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))

    def test_missing_when_nothing_configured(self) -> None:
        self.assertEqual(sl.CREDENTIAL_MISSING, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))

    def test_unsafe_when_home_credential_file_is_malformed(self) -> None:
        cred = credentials_path(self.mozyo_home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("- not\n- a mapping\n", encoding="utf-8")
        os.chmod(cred, 0o600)
        self.assertEqual(sl.CREDENTIAL_UNSAFE, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))

    def test_unsafe_when_home_credential_file_has_loose_permissions(self) -> None:
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX-only permission gate")
        _write_home_credential(self.mozyo_home, mode=0o644)
        self.assertEqual(sl.CREDENTIAL_UNSAFE, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))


class DaemonHomePinTest(_DarwinCase):
    """R2-F1: the install preflight and the launchd daemon resolve the SAME mozyo home."""

    def test_custom_mozyo_home_is_pinned_into_argv(self) -> None:
        _write_home_credential(self.mozyo_home)
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=FakeRunner(), which=_which_found
        )
        self.assertTrue(result["performed"])
        argv = plistlib.loads(sl.plist_path(self.os_home).read_bytes())["ProgramArguments"]
        self.assertIn("--home", argv)
        self.assertEqual(_resolved(self.mozyo_home), argv[argv.index("--home") + 1])

    def test_daemon_side_source_agrees_with_the_pinned_home(self) -> None:
        _write_home_credential(self.mozyo_home)
        ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.os_home))
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNotNone(default_redmine_source(ws, home=self.mozyo_home))
            self.assertIsNone(default_redmine_source(ws, home=self.os_home))  # no credential there


class InstallTest(_DarwinCase):
    def test_install_writes_plist_and_bootstraps_when_home_credential_ready(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=300,
            runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"])
        self.assertEqual(sl.CREDENTIAL_READY, result["credential_readiness"])
        plist_file = sl.plist_path(self.os_home)
        payload = plistlib.loads(plist_file.read_bytes())
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("EnvironmentVariables", payload)
        self.assertEqual(
            [
                ["launchctl", "bootout", f"{_GUI_DOMAIN}/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
                ["launchctl", "bootstrap", _GUI_DOMAIN, str(plist_file)],
            ],
            runner.calls,
        )

    def test_install_is_idempotent(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=300,
                   runner=runner, which=_which_found)
        first = sl.plist_path(self.os_home).read_bytes()
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=300,
                   runner=runner, which=_which_found)
        second = sl.plist_path(self.os_home).read_bytes()
        self.assertEqual(first, second)

    def test_install_refuses_zero_mutation_on_env_only_credential(self) -> None:
        runner = FakeRunner()
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                                runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.os_home).exists())

    def test_install_refuses_zero_mutation_on_non_darwin(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                                runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.os_home).exists())

    def test_install_refuses_zero_mutation_on_missing_executable(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_missing)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_EXECUTABLE_NOT_FOUND, result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.os_home).exists())

    def test_install_refuses_zero_mutation_on_missing_credential(self) -> None:
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.os_home).exists())

    def test_install_refuses_zero_mutation_on_incomplete_credential(self) -> None:
        _write_home_credential(self.mozyo_home, url=None)
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_incomplete", result["reason"])
        self.assertEqual([], runner.calls)

    def test_install_refuses_zero_mutation_on_unsafe_credential(self) -> None:
        cred = credentials_path(self.mozyo_home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("- not a mapping\n", encoding="utf-8")
        os.chmod(cred, 0o600)
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_unsafe", result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.os_home).exists())

    def test_install_pins_absolute_executable_for_relative_which(self) -> None:
        # R5-F1: even a relative PATH resolution is pinned as an absolute path in the plist.
        _write_home_credential(self.mozyo_home)
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=FakeRunner(), which=_which_relative)
        self.assertTrue(result["performed"])
        argv0 = plistlib.loads(sl.plist_path(self.os_home).read_bytes())["ProgramArguments"][0]
        self.assertTrue(Path(argv0).is_absolute())
        self.assertEqual(os.path.abspath("bin/mozyo-bridge"), argv0)

    def test_install_bootstrap_failure_is_reported_without_host_detail(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner(default=_result(1, stderr="boom"))
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_BOOTSTRAP_FAILED, result["reason"])
        self.assertNotIn("boom", str(result))


class RestartTest(_DarwinCase):
    def _install_ready(self) -> None:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, runner=FakeRunner(),
                   which=_which_found)

    def test_restart_kickstarts_loaded_service_using_the_installed_pin(self) -> None:
        self._install_ready()
        runner = FakeRunner(print_result=_result(0, stdout="state = running\n\tpid = 4242\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertTrue(result["performed"])
        self.assertEqual(
            ["launchctl", "kickstart", "-k", f"{_GUI_DOMAIN}/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
            runner.calls[-1],
        )

    def test_restart_refuses_zero_mutation_when_not_loaded(self) -> None:
        self._install_ready()
        runner = FakeRunner(print_result=_result(113, stderr="not found"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_SERVICE_NOT_LOADED, result["reason"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_when_not_installed(self) -> None:
        runner = FakeRunner()
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_NOT_INSTALLED, result["reason"])
        self.assertEqual([], runner.calls)

    def test_restart_checks_the_pinned_home_not_the_current_shell(self) -> None:
        # R3-F1 core: the plist is pinned to A (no credential); a caller with no --home must NOT
        # kickstart just because some other current home would be ready.
        with tempfile.TemporaryDirectory() as a:
            a = Path(a)  # A has NO credential
            _pinned_plist(self.os_home, _resolved(a))
            runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
            result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_on_requested_home_that_differs_from_pin(self) -> None:
        # R3-F1: a --home that disagrees with the installed pin is a re-point attempt -> fail-closed.
        self._install_ready()  # pinned to mozyo_home (ready)
        with tempfile.TemporaryDirectory() as other:
            _write_home_credential(Path(other))  # a DIFFERENT ready home
            runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
            result = sl.restart(os_home=self.os_home, mozyo_home=Path(other), runner=runner,
                                which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_HOME_PIN_MISMATCH, result["reason"])
        self.assertEqual([], runner.calls)

    def test_restart_refuses_on_unhealthy_pin(self) -> None:
        # A plist with no --home pin (e.g. a hand-edited / legacy file) is not trusted.
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(sl.render_plist(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"],
            interval_seconds=300, os_home=self.os_home,
        ))
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_HOME_PIN_UNHEALTHY, result["reason"])
        self.assertEqual([], runner.calls)

    def test_restart_refuses_on_relative_installed_pin(self) -> None:
        # R4-F1: a legacy plist pinning a relative --home is never kickstarted.
        _pinned_plist(self.os_home, "relative-home")
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_HOME_PIN_UNHEALTHY, result["reason"])
        self.assertEqual(sl.HOME_PIN_NOT_ABSOLUTE, result["home_pin"])
        self.assertEqual([], runner.calls)

    def test_restart_refuses_on_installed_executable_drift(self) -> None:
        # R4-F2: the plist pins a now-moved executable; a present current executable must NOT
        # kickstart the stale argv — reinstall to change it.
        _write_home_credential(self.mozyo_home)  # pinned home IS ready
        _pinned_plist(self.os_home, _resolved(self.mozyo_home), executable="/old/missing/mozyo-bridge")
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner,
                            which=lambda _n: "/new/current/mozyo-bridge")
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_INSTALLED_COMMAND_DRIFT, result["reason"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_on_relative_installed_executable(self) -> None:
        # R5-F1: a legacy plist pinning a relative executable is caught by the argv-drift authority.
        _write_home_credential(self.mozyo_home)
        _pinned_plist(self.os_home, _resolved(self.mozyo_home), executable="bin/mozyo-bridge")
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_INSTALLED_COMMAND_DRIFT, result["reason"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_on_unreadable_plist_distinct_from_absent(self) -> None:
        # R4-F3: a present-but-unparseable plist is unhealthy, not "not installed".
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x01 not a plist")
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_HOME_PIN_UNHEALTHY, result["reason"])
        self.assertEqual(sl.HOME_PIN_UNREADABLE, result["home_pin"])
        self.assertEqual([], runner.calls)

    def test_restart_refuses_on_non_darwin(self) -> None:
        self._install_ready()
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)


class UninstallTest(_DarwinCase):
    def test_uninstall_boots_out_and_removes_exactly_owned_plist(self) -> None:
        plist_file = sl.plist_path(self.os_home)
        plist_file.parent.mkdir(parents=True)
        plist_file.write_bytes(b"placeholder")
        bystander = plist_file.parent / "some.other.agent.plist"
        bystander.write_bytes(b"untouched")
        runner = FakeRunner()
        result = sl.uninstall(os_home=self.os_home, runner=runner)
        self.assertTrue(result["performed"])
        self.assertTrue(result["removed"])
        self.assertFalse(plist_file.exists())
        self.assertTrue(bystander.exists())
        self.assertEqual(
            [["launchctl", "bootout", f"{_GUI_DOMAIN}/{sl.SUPERVISOR_LAUNCHD_LABEL}"]],
            runner.calls,
        )

    def test_uninstall_is_safe_without_credential_and_without_plist(self) -> None:
        runner = FakeRunner()
        result = sl.uninstall(os_home=self.os_home, runner=runner)
        self.assertTrue(result["performed"])
        self.assertFalse(result["removed"])

    def test_uninstall_refuses_zero_mutation_on_non_darwin(self) -> None:
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.uninstall(os_home=self.os_home, runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)


class ServiceStatusTest(_DarwinCase):
    def test_status_of_installed_loaded_service_is_redacted_projection(self) -> None:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=120,
                   runner=FakeRunner(), which=_which_found)
        runner = FakeRunner(print_result=_result(0, stdout="state = running\n\tpid = 4242\n"))
        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertEqual(4242, status["pid"])
        self.assertEqual(120, status["scheduled_interval_seconds"])
        self.assertTrue(status["run_at_load"])
        self.assertFalse(status["keep_alive_present"])
        self.assertTrue(status["no_environment_block"])
        self.assertEqual(sl.HOME_PIN_OK, status["home_pin"])
        self.assertTrue(status["executable_matches"])
        self.assertEqual(sl.CREDENTIAL_READY, status["credential_readiness"])
        blob = str(status)
        self.assertNotIn(str(self.os_home), blob)
        self.assertNotIn(str(self.mozyo_home), blob)
        self.assertNotIn(_resolved(self.mozyo_home), blob)
        self.assertNotIn("home-key", blob.lower())
        self.assertNotIn("x-redmine-api-key", blob.lower())

    def test_status_reports_the_pinned_home_readiness_not_the_current_shell(self) -> None:
        # R3-F1: installed pin (mozyo_home, ready); a DIFFERENT current home B (missing) must not
        # change the projection — it reflects the installed daemon's pinned root.
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, runner=FakeRunner(),
                   which=_which_found)
        with tempfile.TemporaryDirectory() as b:  # B: no credential
            status = sl.service_status(
                os_home=self.os_home, mozyo_home=Path(b),
                runner=FakeRunner(print_result=_result(113)), which=_which_found,
            )
        self.assertEqual(sl.CREDENTIAL_READY, status["credential_readiness"])  # A's, not B's

    def test_status_flags_unhealthy_pin_with_empty_readiness(self) -> None:
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(sl.render_plist(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"],
            interval_seconds=300, os_home=self.os_home,
        ))
        status = sl.service_status(
            os_home=self.os_home, runner=FakeRunner(print_result=_result(113)), which=_which_found
        )
        self.assertEqual(sl.HOME_PIN_MISSING, status["home_pin"])
        self.assertEqual("", status["credential_readiness"])
        self.assertFalse(status["executable_matches"])

    def test_status_flags_relative_pin_unhealthy(self) -> None:
        # R4-F1: an installed relative pin is unhealthy in the projection too.
        _pinned_plist(self.os_home, "relative-home")
        status = sl.service_status(
            os_home=self.os_home, runner=FakeRunner(print_result=_result(113)), which=_which_found
        )
        self.assertTrue(status["installed"])
        self.assertEqual(sl.HOME_PIN_NOT_ABSOLUTE, status["home_pin"])
        self.assertEqual("", status["credential_readiness"])

    def test_status_distinguishes_unreadable_plist_from_absent(self) -> None:
        # R4-F3: a present-but-unparseable plist is installed=True + unreadable_plist + empty
        # readiness — never the not_installed / would-be-root projection.
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x01 not a plist")
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            status = sl.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=FakeRunner(print_result=_result(113)), which=_which_found,
            )
        self.assertTrue(status["installed"])
        self.assertEqual(sl.HOME_PIN_UNREADABLE, status["home_pin"])
        self.assertEqual("", status["credential_readiness"])
        self.assertFalse(status["executable_matches"])

    def test_status_when_not_installed_reports_would_be_root_and_hint_interval(self) -> None:
        runner = FakeRunner(print_result=_result(113))
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            status = sl.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home, interval_hint=300,
                runner=runner, which=_which_found,
            )
        self.assertFalse(status["installed"])
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["pid"])
        self.assertEqual(sl.HOME_PIN_NOT_INSTALLED, status["home_pin"])
        self.assertEqual(300, status["scheduled_interval_seconds"])
        self.assertFalse(status["executable_matches"])
        self.assertEqual(sl.CREDENTIAL_MISSING, status["credential_readiness"])

    def test_status_survives_absent_launchctl(self) -> None:
        def no_launchctl(_argv):
            raise FileNotFoundError("launchctl")

        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=no_launchctl, which=_which_missing
        )
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["pid"])

    def test_status_flags_relative_installed_executable(self) -> None:
        # R5-F1: an installed relative executable reads as executable_matches=False in the projection.
        _write_home_credential(self.mozyo_home)
        _pinned_plist(self.os_home, _resolved(self.mozyo_home), executable="bin/mozyo-bridge")
        status = sl.service_status(
            os_home=self.os_home, runner=FakeRunner(print_result=_result(113)), which=_which_found
        )
        self.assertFalse(status["executable_matches"])

    def test_status_flags_executable_drift(self) -> None:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, runner=FakeRunner(),
                   which=_which_found)

        def which_moved(_name):
            return "/some/other/path/mozyo-bridge"

        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=_result(113)), which=which_moved,
        )
        self.assertFalse(status["executable_matches"])


if __name__ == "__main__":
    unittest.main()
