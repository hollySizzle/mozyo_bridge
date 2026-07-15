"""macOS LaunchAgent lifecycle tests for the callback supervisor (Redmine #13683 Phase B1).

Real launchctl is never invoked and the real host LaunchAgents dir is never touched: every
subprocess call goes through an injected fake runner, and temp roots stand in for the OS user home
(``os_home``: plist/log) and the mozyo home (``mozyo_home``: credential/registry root) — two roots
that are kept **distinct** (review j#79092 R2-F1). These pin the Phase B1 safety boundary —

- plist structure: no ``EnvironmentVariables`` key, ``RunAtLoad`` + ``StartInterval``, **no**
  ``KeepAlive``, exact PATH-resolved executable argv with the mozyo home pinned as ``--home``;
- structured launchctl argv (bootout-then-bootstrap install, kickstart -k restart, exact-file
  uninstall), idempotent install;
- fail-closed **zero-mutation** refusals: non-darwin host, missing executable, and the Redmine
  credential matrix — where readiness is **daemon-effective** (the mozyo-home credential file the
  launchd agent will actually see; neither an installer's shell key/URL (j#79059 F1) nor a shell
  ``MOZYO_BRIDGE_HOME`` (j#79092 R2-F1) can make it ``ready``);
- the install-preflight and daemon-runtime resolve the **same** mozyo home (the pinned ``--home``);
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
SHELL_ENV = {API_KEY_ENV: "shell-key-value", BASE_URL_ENV: "https://redmine.shell.test"}


def _write_home_credential(mozyo_home: Path, *, api_key="home-key", url="https://redmine.example.test",
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
    """Base: force darwin + a fixed uid, and provide distinct os_home / mozyo_home temp roots."""

    def setUp(self) -> None:
        self._tmp_os = tempfile.TemporaryDirectory()
        self._tmp_mozyo = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_os.cleanup)
        self.addCleanup(self._tmp_mozyo.cleanup)
        self.os_home = Path(self._tmp_os.name)
        self.mozyo_home = Path(self._tmp_mozyo.name)
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
    def test_pins_executable_and_resolved_mozyo_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = sl.resolve_supervisor_command(mozyo_home=Path(tmp), which=_which_found)
        self.assertEqual(
            ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", tmp],
            cmd,
        )

    def test_missing_executable_is_none_not_a_shell_string(self) -> None:
        self.assertIsNone(sl.resolve_supervisor_command(which=_which_missing))


class CredentialReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mozyo_home = Path(self._tmp.name)

    def test_ready_only_from_secure_home_credential_file(self) -> None:
        _write_home_credential(self.mozyo_home)
        self.assertEqual(sl.CREDENTIAL_READY, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))

    def test_env_only_is_not_ready_daemon_never_sees_shell_env(self) -> None:
        # F1 regression guard: shell key/URL but no home file must NOT be ready.
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            self.assertEqual(
                sl.CREDENTIAL_MISSING, sl.classify_credential_readiness(mozyo_home=self.mozyo_home)
            )

    def test_shell_mozyo_home_override_does_not_leak_into_readiness(self) -> None:
        # R2-F1 regression guard: a shell MOZYO_BRIDGE_HOME pointing at a credential-bearing root
        # must NOT make an explicitly-scoped (credential-less) mozyo home read as ready.
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
        _write_home_credential(self.mozyo_home, mode=0o644)  # group/other readable -> fail-closed
        self.assertEqual(sl.CREDENTIAL_UNSAFE, sl.classify_credential_readiness(mozyo_home=self.mozyo_home))


class DaemonHomePinTest(_DarwinCase):
    """R2-F1: the install preflight and the launchd daemon resolve the SAME mozyo home."""

    def test_custom_mozyo_home_is_pinned_into_argv(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(result["performed"])
        argv = plistlib.loads(sl.plist_path(self.os_home).read_bytes())["ProgramArguments"]
        self.assertIn("--home", argv)
        # The exact mozyo home is pinned so the daemon reads the same credential root — not a
        # default ~/.mozyo_bridge it would otherwise re-derive with no MOZYO_BRIDGE_HOME.
        self.assertEqual(str(self.mozyo_home), argv[argv.index("--home") + 1])

    def test_daemon_side_source_agrees_with_the_pinned_home(self) -> None:
        # End-to-end: default_redmine_source(home=pinned) builds a live source from the same file
        # the install preflight validated; a different (empty) home yields None (the divergence
        # the pin closes). Shell env is cleared so only the home file decides.
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
        self.assertTrue(plist_file.exists())
        payload = plistlib.loads(plist_file.read_bytes())
        self.assertNotIn("KeepAlive", payload)
        self.assertNotIn("EnvironmentVariables", payload)
        self.assertEqual(
            [
                ["launchctl", "bootout", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
                ["launchctl", "bootstrap", "gui/501", str(plist_file)],
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
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
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
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
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

    def test_install_bootstrap_failure_is_reported_without_host_detail(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner(default=_result(1, stderr="boom"))
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_BOOTSTRAP_FAILED, result["reason"])
        self.assertNotIn("boom", str(result))


class RestartTest(_DarwinCase):
    def test_restart_kickstarts_loaded_service(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner(print_result=_result(0, stdout="state = running\n\tpid = 4242\n"))
        result = sl.restart(mozyo_home=self.mozyo_home, runner=runner, which=_which_found)
        self.assertTrue(result["performed"])
        self.assertIn("kickstart", runner.verbs)
        self.assertEqual(
            ["launchctl", "kickstart", "-k", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"],
            runner.calls[-1],
        )

    def test_restart_refuses_zero_mutation_when_not_loaded(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner(print_result=_result(113, stderr="not found"))
        result = sl.restart(mozyo_home=self.mozyo_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_SERVICE_NOT_LOADED, result["reason"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_refuses_on_env_only_credential_before_probe(self) -> None:
        runner = FakeRunner(print_result=_result(0, stdout="pid = 1\n"))
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            result = sl.restart(mozyo_home=self.mozyo_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual("redmine_credential_missing", result["reason"])
        self.assertEqual([], runner.calls)  # refused before the loaded-probe read

    def test_restart_refuses_on_non_darwin_without_any_launchctl(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.restart(mozyo_home=self.mozyo_home, runner=runner, which=_which_found)
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
            [["launchctl", "bootout", f"gui/501/{sl.SUPERVISOR_LAUNCHD_LABEL}"]],
            runner.calls,
        )

    def test_uninstall_is_safe_without_credential_and_without_plist(self) -> None:
        runner = FakeRunner()
        result = sl.uninstall(os_home=self.os_home, runner=runner)  # no cred, no plist
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
        self.assertTrue(status["executable_matches"])
        self.assertEqual(sl.CREDENTIAL_READY, status["credential_readiness"])
        # Redacted: no path, no secret, no request header anywhere in the projection.
        blob = str(status)
        self.assertNotIn(str(self.os_home), blob)
        self.assertNotIn(str(self.mozyo_home), blob)
        self.assertNotIn("home-key", blob.lower())
        self.assertNotIn("x-redmine-api-key", blob.lower())

    def test_status_reports_missing_for_env_only_credential(self) -> None:
        runner = FakeRunner(print_result=_result(113))
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            status = sl.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home, interval_hint=300,
                runner=runner, which=_which_found,
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
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=no_launchctl, which=_which_missing
        )
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["pid"])

    def test_status_flags_executable_drift(self) -> None:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home, interval_seconds=120,
                   runner=FakeRunner(), which=_which_found)

        def which_moved(_name):
            return "/some/other/path/mozyo-bridge"

        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=_result(113)), which=which_moved,
        )
        self.assertFalse(status["executable_matches"])


if __name__ == "__main__":
    unittest.main()
