"""macOS LaunchAgent lifecycle tests for the callback supervisor (Redmine #13683 Phase B1).

Real launchctl is never invoked and the real host LaunchAgents dir is never touched: every
subprocess call goes through an injected fake runner, and a temp home stands in for ``~``. These
pin the Phase B1 safety boundary —

- plist structure: no ``EnvironmentVariables`` key, ``RunAtLoad`` + ``StartInterval``, **no**
  ``KeepAlive``, exact PATH-resolved executable argv;
- structured launchctl argv (bootout-then-bootstrap install, kickstart -k restart, exact-file
  uninstall), idempotent install;
- fail-closed **zero-mutation** refusals: non-darwin host, missing executable, and the Redmine
  credential matrix (missing / incomplete / unsafe-malformed / ready);
- a redacted status projection (booleans / counts / fixed tokens; no secret, no path);

without touching the host. Live launchd operation is a separate coordinator gate (never here).
"""

from __future__ import annotations

import os
import plistlib
import stat
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
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context import (
    API_KEY_ENV,
    BASE_URL_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
    credentials_path,
)

READY_ENV = {API_KEY_ENV: "secret-key-value", BASE_URL_ENV: "https://redmine.example.test"}


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


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


class _DarwinCase(unittest.TestCase):
    """Base: force darwin + a fixed uid so target strings are deterministic on any host."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        p_darwin = patch.object(sl, "_running_on_darwin", return_value=True)
        p_uid = patch.object(sl.os, "getuid", return_value=501, create=True)
        p_darwin.start()
        p_uid.start()
        self.addCleanup(p_darwin.stop)
        self.addCleanup(p_uid.stop)


class RenderPlistTest(unittest.TestCase):
    def test_plist_is_one_shot_scheduled_and_carries_no_environment_or_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = sl.render_plist(
                ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"],
                interval_seconds=300,
                home=Path(tmp),
            )
        payload = plistlib.loads(raw)
        self.assertEqual(sl.SUPERVISOR_LAUNCHD_LABEL, payload["Label"])
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(300, payload["StartInterval"])
        # One-shot scheduled: KeepAlive must be structurally absent (not merely false).
        self.assertNotIn("KeepAlive", payload)
        # Credential boundary: no environment block exists at all.
        self.assertNotIn("EnvironmentVariables", payload)
        text = raw.decode("utf-8")
        for token in ("API_KEY", "REDMINE", "TOKEN", "SECRET"):
            self.assertNotIn(token, text)

    def test_interval_is_clamped_to_at_least_one(self) -> None:
        payload = plistlib.loads(
            sl.render_plist(["/opt/bin/mozyo-bridge"], interval_seconds=0)
        )
        self.assertEqual(1, payload["StartInterval"])

    def test_secret_in_daemon_env_never_serializes_into_plist(self) -> None:
        with patch.dict("os.environ", {API_KEY_ENV: "SECRET-KEY-VALUE"}, clear=False):
            raw = sl.render_plist(["/opt/bin/mozyo-bridge"], interval_seconds=300)
        self.assertNotIn(b"SECRET-KEY-VALUE", raw)


class ResolveCommandTest(unittest.TestCase):
    def test_resolves_exact_executable_with_structured_argv(self) -> None:
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once"],
            sl.resolve_supervisor_command(which=_which_found),
        )

    def test_missing_executable_is_none_not_a_shell_string(self) -> None:
        self.assertIsNone(sl.resolve_supervisor_command(which=_which_missing))


class CredentialReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_ready_when_key_and_url_present(self) -> None:
        self.assertEqual(
            sl.CREDENTIAL_READY,
            sl.classify_credential_readiness(home=self.home, environ=READY_ENV),
        )

    def test_incomplete_when_only_key(self) -> None:
        self.assertEqual(
            sl.CREDENTIAL_INCOMPLETE,
            sl.classify_credential_readiness(home=self.home, environ={API_KEY_ENV: "k"}),
        )

    def test_missing_when_nothing_configured(self) -> None:
        self.assertEqual(
            sl.CREDENTIAL_MISSING,
            sl.classify_credential_readiness(home=self.home, environ={}),
        )

    def test_unsafe_when_home_credential_file_is_malformed(self) -> None:
        cred = credentials_path(self.home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("- not\n- a mapping\n", encoding="utf-8")
        os.chmod(cred, 0o600)
        self.assertEqual(
            sl.CREDENTIAL_UNSAFE,
            sl.classify_credential_readiness(home=self.home, environ={}),
        )

    def test_unsafe_when_home_credential_file_has_loose_permissions(self) -> None:
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX-only permission gate")
        cred = credentials_path(self.home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("redmine:\n  api_key: k\n  url: https://r.example\n", encoding="utf-8")
        os.chmod(cred, 0o644)  # group/other readable -> fail-closed
        self.assertEqual(
            sl.CREDENTIAL_UNSAFE,
            sl.classify_credential_readiness(home=self.home, environ={}),
        )


class InstallTest(_DarwinCase):
    def test_install_writes_plist_and_bootstraps_when_ready(self) -> None:
        runner = FakeRunner()
        result = sl.install(
            home=self.home, interval_seconds=300, environ=READY_ENV,
            runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"])
        self.assertEqual(sl.CREDENTIAL_READY, result["credential_readiness"])
        plist_file = sl.plist_path(self.home)
        self.assertTrue(plist_file.exists())
        payload = plistlib.loads(plist_file.read_bytes())
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("EnvironmentVariables", payload)
        # bootout (ignore-failure) then bootstrap, structured argv only.
        self.assertEqual(
            [
                ["launchctl", "bootout", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
                ["launchctl", "bootstrap", "gui/501", str(plist_file)],
            ],
            runner.calls,
        )

    def test_install_is_idempotent(self) -> None:
        runner = FakeRunner()
        sl.install(home=self.home, interval_seconds=300, environ=READY_ENV,
                   runner=runner, which=_which_found)
        first = sl.plist_path(self.home).read_bytes()
        sl.install(home=self.home, interval_seconds=300, environ=READY_ENV,
                   runner=runner, which=_which_found)
        second = sl.plist_path(self.home).read_bytes()
        self.assertEqual(first, second)

    def test_install_refuses_zero_mutation_on_non_darwin(self) -> None:
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.install(home=self.home, environ=READY_ENV, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)  # no launchctl
        self.assertFalse(sl.plist_path(self.home).exists())  # no plist written

    def test_install_refuses_zero_mutation_on_missing_executable(self) -> None:
        runner = FakeRunner()
        result = sl.install(home=self.home, environ=READY_ENV, runner=runner, which=_which_missing)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_EXECUTABLE_NOT_FOUND, result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.home).exists())

    def test_install_refuses_zero_mutation_on_missing_credential(self) -> None:
        runner = FakeRunner()
        result = sl.install(home=self.home, environ={}, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.home).exists())

    def test_install_refuses_zero_mutation_on_incomplete_credential(self) -> None:
        runner = FakeRunner()
        result = sl.install(home=self.home, environ={API_KEY_ENV: "k"}, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_incomplete", result["reason"])
        self.assertEqual([], runner.calls)

    def test_install_refuses_zero_mutation_on_unsafe_credential(self) -> None:
        cred = credentials_path(self.home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("- not a mapping\n", encoding="utf-8")
        os.chmod(cred, 0o600)
        runner = FakeRunner()
        result = sl.install(home=self.home, environ={}, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_unsafe", result["reason"])
        self.assertEqual([], runner.calls)
        self.assertFalse(sl.plist_path(self.home).exists())

    def test_install_bootstrap_failure_is_reported(self) -> None:
        runner = FakeRunner(default=_result(1, stderr="boom"))
        result = sl.install(home=self.home, environ=READY_ENV, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_BOOTSTRAP_FAILED, result["reason"])
        # The plist was written, but the redacted reason carries no host detail.
        self.assertNotIn("boom", str(result))


class RestartTest(_DarwinCase):
    def test_restart_kickstarts_loaded_service(self) -> None:
        runner = FakeRunner(print_result=_result(0, stdout="state = running\n\tpid = 4242\n"))
        result = sl.restart(home=self.home, environ=READY_ENV, runner=runner, which=_which_found)
        self.assertTrue(result["performed"])
        self.assertIn("kickstart", runner.verbs)
        self.assertEqual(
            ["launchctl", "kickstart", "-k", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
            runner.calls[-1],
        )

    def test_restart_refuses_zero_mutation_when_not_loaded(self) -> None:
        runner = FakeRunner(print_result=_result(113, stderr="not found"))
        result = sl.restart(home=self.home, environ=READY_ENV, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_SERVICE_NOT_LOADED, result["reason"])
        # print (the read) may run, but kickstart (the mutation) must not.
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_on_non_darwin_without_any_launchctl(self) -> None:
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.restart(home=self.home, environ=READY_ENV, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)

    def test_restart_refuses_on_missing_credential_before_probe(self) -> None:
        runner = FakeRunner(print_result=_result(0, stdout="pid = 1\n"))
        result = sl.restart(home=self.home, environ={}, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertEqual([], runner.calls)  # refused before the loaded-probe read


class UninstallTest(_DarwinCase):
    def test_uninstall_boots_out_and_removes_exactly_owned_plist(self) -> None:
        plist_file = sl.plist_path(self.home)
        plist_file.parent.mkdir(parents=True)
        plist_file.write_bytes(b"placeholder")
        bystander = plist_file.parent / "some.other.agent.plist"
        bystander.write_bytes(b"untouched")
        runner = FakeRunner()
        result = sl.uninstall(home=self.home, runner=runner)
        self.assertTrue(result["performed"])
        self.assertTrue(result["removed"])
        self.assertFalse(plist_file.exists())
        self.assertTrue(bystander.exists())
        self.assertEqual(
            [["launchctl", "bootout", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"]],
            runner.calls,
        )

    def test_uninstall_is_safe_without_credential_and_without_plist(self) -> None:
        runner = FakeRunner()
        result = sl.uninstall(home=self.home, runner=runner)  # no env, no plist
        self.assertTrue(result["performed"])
        self.assertFalse(result["removed"])

    def test_uninstall_refuses_zero_mutation_on_non_darwin(self) -> None:
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.uninstall(home=self.home, runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_UNSUPPORTED_PLATFORM, result["reason"])
        self.assertEqual([], runner.calls)


class ServiceStatusTest(_DarwinCase):
    def test_status_of_installed_loaded_service_is_redacted_projection(self) -> None:
        sl.install(home=self.home, interval_seconds=120, environ=READY_ENV,
                   runner=FakeRunner(), which=_which_found)
        runner = FakeRunner(print_result=_result(0, stdout="state = running\n\tpid = 4242\n"))
        status = sl.service_status(
            home=self.home, environ=READY_ENV, runner=runner, which=_which_found
        )
        self.assertTrue(status["installed"])
        self.assertTrue(status["loaded"])
        self.assertEqual(4242, status["pid"])
        self.assertEqual(120, status["scheduled_interval_seconds"])
        self.assertTrue(status["run_at_load"])
        self.assertFalse(status["keep_alive_present"])
        self.assertTrue(status["no_environment_block"])
        self.assertTrue(status["executable_matches"])
        self.assertEqual(sl.CREDENTIAL_READY, status["credential_readiness"])
        # Redacted: no path, no secret, no request header anywhere in the projection.
        blob = str(status)
        self.assertNotIn(str(self.home), blob)
        self.assertNotIn("secret-key-value", blob.lower())
        self.assertNotIn("x-redmine-api-key", blob.lower())

    def test_status_when_not_installed_reports_hint_interval_and_missing_credential(self) -> None:
        runner = FakeRunner(print_result=_result(113))
        status = sl.service_status(
            home=self.home, interval_hint=300, environ={}, runner=runner, which=_which_found
        )
        self.assertFalse(status["installed"])
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["pid"])
        self.assertEqual(300, status["scheduled_interval_seconds"])  # the would-be interval
        self.assertFalse(status["executable_matches"])
        self.assertEqual(sl.CREDENTIAL_MISSING, status["credential_readiness"])

    def test_status_survives_absent_launchctl(self) -> None:
        def no_launchctl(_argv):
            raise FileNotFoundError("launchctl")

        status = sl.service_status(
            home=self.home, environ={}, runner=no_launchctl, which=_which_missing
        )
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["pid"])

    def test_status_flags_executable_drift(self) -> None:
        sl.install(home=self.home, interval_seconds=120, environ=READY_ENV,
                   runner=FakeRunner(), which=_which_found)

        def which_moved(_name):
            return "/some/other/path/mozyo-bridge"

        status = sl.service_status(
            home=self.home, environ=READY_ENV, runner=FakeRunner(print_result=_result(113)),
            which=which_moved,
        )
        self.assertFalse(status["executable_matches"])


if __name__ == "__main__":
    unittest.main()
