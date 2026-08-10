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

    def test_install_is_not_gated_on_a_shell_only_credential(self) -> None:
        # A launchd agent never sees the installer's shell env, so readiness resolves against the
        # home file and reads `missing` here. Since j#102151 Finding 4 that is REPORTED, not gated:
        # a tick still does useful local work from SQLite + Herdr with no provider at all.
        runner = FakeRunner()
        with patch.dict("os.environ", SHELL_ENV, clear=False):
            result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                                runner=runner, which=_which_found)
        self.assertTrue(result["performed"], result)
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
        self.assertTrue(sl.plist_path(self.os_home).exists())

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

    def test_install_is_not_gated_on_a_missing_credential(self) -> None:
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertTrue(result["performed"], result)
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
        self.assertTrue(sl.plist_path(self.os_home).exists())

    def test_install_is_not_gated_on_an_incomplete_credential(self) -> None:
        _write_home_credential(self.mozyo_home, url=None)
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertTrue(result["performed"], result)
        self.assertEqual(sl.CREDENTIAL_INCOMPLETE, result["credential_readiness"])

    def test_install_is_not_gated_on_an_unsafe_credential(self) -> None:
        # An unsafe file is reported, and installing does not "use" it: the resolver refuses to read
        # it and hands back no value, so the timer runs local-only work exactly as with none at all.
        cred = credentials_path(self.mozyo_home)
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("- not a mapping\n", encoding="utf-8")
        os.chmod(cred, 0o600)
        runner = FakeRunner()
        result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                            runner=runner, which=_which_found)
        self.assertTrue(result["performed"], result)
        self.assertEqual(sl.CREDENTIAL_UNSAFE, result["credential_readiness"])
        # The install neither repairs nor bypasses the unsafe file, and leaks nothing from it.
        self.assertNotIn("not a mapping", str(result))

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

    def test_restart_reports_the_pinned_home_readiness_not_the_current_shell(self) -> None:
        # R3-F1 core, carried into the non-gating contract (j#102151 Finding 4). Readiness no longer
        # decides whether restart runs, but it must still DESCRIBE the home the loaded service is
        # actually pinned to. The plist is pinned to A (no credential) while a different, fully
        # ready home exists; reporting `ready` here would mean the projection had drifted back to
        # the caller's home, which is the exact defect R3-F1 closed.
        _write_home_credential(self.mozyo_home)  # a DIFFERENT home that IS ready
        with tempfile.TemporaryDirectory() as a:
            a = Path(a)  # A has NO credential
            _pinned_plist(self.os_home, _resolved(a))
            runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
            result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertTrue(result["performed"], result)
        self.assertEqual(sl.CREDENTIAL_MISSING, result["credential_readiness"])
        self.assertIn("kickstart", runner.verbs)

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
        self.assertNotIn("home-key", blob.lower())
        self.assertNotIn("x-redmine-api-key", blob.lower())
        # The mozyo home appears in exactly ONE place: the `installed_command` argv added by #15192
        # so 実行内容 reads the same on both hosts (the Linux adapter has published it since #15183).
        # That value is a config DIRECTORY, not a credential — the key and URL live in a file under
        # it and never reach this projection, as the two assertions above pin. Everything else stays
        # path-free, which is what this test guards: narrowed to the carve-out, not dropped.
        without_command = {k: v for k, v in status.items() if k != "installed_command"}
        self.assertNotIn(str(self.mozyo_home), str(without_command))
        self.assertNotIn(_resolved(self.mozyo_home), str(without_command))
        self.assertIn(_resolved(self.mozyo_home), status["installed_command"])

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


def _legacy_drain_plist(os_home: Path, *, label: str | None = None) -> Path:
    """Write a pre-#15192 drain LaunchAgent at its owned path (test double for an old install)."""
    target = sl.plist_path(os_home, agent=sl.LEGACY_DRAIN_AGENT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        plistlib.dumps(
            {
                "Label": sl.LEGACY_DRAIN_AGENT.label if label is None else label,
                "ProgramArguments": ["/opt/bin/mozyo-bridge", "workflow", "supervisor",
                                     "--drain-only", "--home", "/tmp/x"],
                "RunAtLoad": True,
                "StartInterval": 60,
            }
        )
    )
    return target


class _LegacyStaysLoaded:
    """launchctl where the RETIRED label is still loaded after its bootout (j#102151 Finding 1)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        target = argv[2] if len(argv) > 2 else ""
        if argv[1] == "bootout" and sl.LEGACY_DRAIN_AGENT.label in target:
            return _result(1)  # bootout failed
        if argv[1] == "print" and sl.LEGACY_DRAIN_AGENT.label in target:
            return _result(0, "\tstate = running\n\tpid = 999\n")  # ...and it is STILL loaded
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) >= 2]


#: What launchctl actually answers for a label it does not know: a distinct exit code AND a message
#: naming the condition. The fake carries BOTH because the adapter accepts either as the positive
#: "no such service" signal — and because a bare non-zero (what this fake used to return) is the
#: AMBIGUOUS case, not the absent one. Modelling it as ambiguous is what review j#102180 finding 1
#: showed was missing: an under-specified fake made an unreadable state look like a verified stop.
_NOT_FOUND = (113, 'Could not find service "org.mozyo-bridge.callback-supervisor.drain" in domain')


class _LegacyNeverLoaded:
    """launchctl where the RETIRED label was never loaded: bootout fails, print says NOT FOUND."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        target = argv[2] if len(argv) > 2 else ""
        if sl.LEGACY_DRAIN_AGENT.label in target:
            code, message = _NOT_FOUND
            return _result(code, stderr=message)
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) >= 2]


class _LegacyUnreadable:
    """launchctl where the RETIRED label's state cannot be READ (permission denied / manager error).

    The exact shape review j#102180 finding 1 reproduced: bootout AND the follow-up print both fail
    without saying the service is unknown. "I could not see it" must not read as "it is gone".
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        target = argv[2] if len(argv) > 2 else ""
        if sl.LEGACY_DRAIN_AGENT.label in target:
            return _result(1, stderr="Operation not permitted")
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) >= 2]


class SingleOwnedAgentTest(_DarwinCase):
    """Redmine #15192: macOS registers exactly ONE LaunchAgent, running the bounded ``--run-once``."""

    def test_exactly_one_owned_agent_running_run_once(self) -> None:
        self.assertEqual(len(sl.SUPERVISOR_AGENTS), 1)
        self.assertIs(sl.SUPERVISOR_AGENTS[0], sl.SUPERVISOR_AGENT)
        self.assertEqual(sl.SUPERVISOR_AGENT.argv_tail[-1], "--run-once")

    def test_the_retired_drain_agent_is_not_an_owned_agent(self) -> None:
        # It still has an identity (the migration needs one) but no verb installs or reports it.
        self.assertNotIn(sl.LEGACY_DRAIN_AGENT, sl.SUPERVISOR_AGENTS)
        self.assertEqual(sl.LEGACY_DRAIN_AGENT.argv_tail[-1], "--drain-only")
        self.assertNotEqual(sl.LEGACY_DRAIN_AGENT.label, sl.SUPERVISOR_AGENT.label)

    def test_install_registers_one_agent_only(self) -> None:
        _write_home_credential(self.mozyo_home)
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"])
        self.assertEqual(runner.verbs.count("bootstrap"), 1)
        self.assertTrue(sl.plist_path(self.os_home).exists())
        self.assertFalse(sl.plist_path(self.os_home, agent=sl.LEGACY_DRAIN_AGENT).exists())

    def test_the_portable_default_interval_is_shared_with_the_linux_adapter(self) -> None:
        # One operator-facing cadence knob: the same number reaches a launchd StartInterval and a
        # systemd OnUnitActiveSec, so `--tick-interval` means one thing (#15192).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_systemd as ss,
        )

        self.assertEqual(
            sl.SUPERVISOR_AGENT.default_interval_seconds, ss.DEFAULT_TICK_INTERVAL_SECONDS
        )
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                   runner=FakeRunner(), which=_which_found)
        installed = plistlib.loads(sl.plist_path(self.os_home).read_bytes())
        self.assertEqual(installed["StartInterval"], ss.DEFAULT_TICK_INTERVAL_SECONDS)

    def test_the_os_tick_stays_finer_than_the_provider_cadence(self) -> None:
        # Equal values would make the OS tick read as a Redmine poll and would align the tick with
        # the watermark, so a just-missed due window waits a whole extra period.
        self.assertLess(
            sl.SUPERVISOR_AGENT.default_interval_seconds, sl.DEFAULT_RECONCILIATION_INTERVAL_SECONDS
        )


class LegacyDrainMigrationTest(_DarwinCase):
    """Redmine #15192: the retired ``--drain-only`` registration is migrated away, under an identity fence."""

    def test_classification_distinguishes_absent_owned_foreign_and_unreadable(self) -> None:
        self.assertEqual(sl.classify_legacy_drain(self.os_home), sl.LEGACY_DRAIN_ABSENT)
        _legacy_drain_plist(self.os_home)
        self.assertEqual(sl.classify_legacy_drain(self.os_home), sl.LEGACY_DRAIN_OWNED)
        _legacy_drain_plist(self.os_home, label="com.example.someone-else")
        self.assertEqual(sl.classify_legacy_drain(self.os_home), sl.LEGACY_DRAIN_FOREIGN)
        sl.plist_path(self.os_home, agent=sl.LEGACY_DRAIN_AGENT).write_bytes(b"not a plist")
        self.assertEqual(sl.classify_legacy_drain(self.os_home), sl.LEGACY_DRAIN_UNREADABLE)

    def test_install_removes_an_owned_legacy_agent_and_leaves_one_registration(self) -> None:
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = _LegacyNeverLoaded()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"], result)
        self.assertEqual(result["legacy_drain"], sl.LEGACY_DRAIN_OWNED)
        self.assertTrue(result["legacy_drain_removed"])
        self.assertFalse(legacy.exists())
        self.assertTrue(sl.plist_path(self.os_home).exists())
        # The retired agent was UNLOADED before its file was unlinked; unlinking alone would leave a
        # bootstrapped service running until logout.
        booted_out = [c for c in runner.calls if len(c) >= 3 and c[1] == "bootout"]
        self.assertIn(f"{_GUI_DOMAIN}/{sl.LEGACY_DRAIN_AGENT.label}", [c[2] for c in booted_out])

    def test_install_refuses_zero_mutation_on_a_foreign_plist_at_the_legacy_path(self) -> None:
        _write_home_credential(self.mozyo_home)
        foreign = _legacy_drain_plist(self.os_home, label="com.example.someone-else")
        before = foreign.read_bytes()
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_FOREIGN_LABEL)
        # Zero mutation: the stranger's plist is intact, our agent was not written, launchctl unused.
        self.assertEqual(foreign.read_bytes(), before)
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertEqual(runner.calls, [])

    def test_install_refuses_zero_mutation_on_an_unreadable_plist_at_the_legacy_path(self) -> None:
        _write_home_credential(self.mozyo_home)
        target = _legacy_drain_plist(self.os_home)
        target.write_bytes(b"not a plist")
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_UNREADABLE)
        self.assertTrue(target.exists())
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertEqual(runner.calls, [])

    def test_a_failed_install_after_migration_never_leaves_two_registrations(self) -> None:
        # The ordering invariant: migrate first, install second. A bootstrap failure mid-sequence
        # can leave zero or one registration — never the two-agent state #15192 exists to end.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)

        class _MigrationOkBootstrapFails(_LegacyNeverLoaded):
            def __call__(self, argv):
                result = super().__call__(argv)
                argv = list(argv)
                if len(argv) >= 2 and argv[1] == "bootstrap":
                    return _result(1)
                return result

        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=_MigrationOkBootstrapFails(), which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_BOOTSTRAP_FAILED)
        self.assertFalse(legacy.exists())

    def test_uninstall_removes_the_owned_agent_and_the_legacy_one(self) -> None:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                   runner=FakeRunner(), which=_which_found)
        legacy = _legacy_drain_plist(self.os_home)  # e.g. re-created by an old build
        result = sl.uninstall(os_home=self.os_home, runner=_LegacyNeverLoaded())
        self.assertTrue(result["performed"])
        self.assertTrue(result["legacy_drain_removed"])
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertFalse(legacy.exists())

    def test_uninstall_over_a_foreign_legacy_plist_still_removes_our_own_agent(self) -> None:
        # Refusing here would strand OUR registration over a file we do not own.
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                   runner=FakeRunner(), which=_which_found)
        foreign = _legacy_drain_plist(self.os_home, label="com.example.someone-else")
        result = sl.uninstall(os_home=self.os_home, runner=FakeRunner())
        self.assertTrue(result["performed"])
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(result["legacy_drain_reason"], sl.REASON_LEGACY_DRAIN_FOREIGN_LABEL)

    def test_a_legacy_agent_that_survives_bootout_blocks_the_install(self) -> None:
        # Review j#102151 Finding 1. Unlinking a plist does NOT unregister a bootstrapped job —
        # launchd keys it off the label — so if the retired job is still loaded after the bootout,
        # installing the new agent would produce TWO live registrations. The stop must be verified.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = _LegacyStaysLoaded()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STILL_LOADED)
        # The owned agent was never written or bootstrapped...
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertNotIn("bootstrap", runner.verbs)
        # ...and the legacy plist is deliberately KEPT: it is the operator's only durable record of
        # the live registration still to deal with, and deleting it would hide a running job.
        self.assertTrue(legacy.exists())

    def test_an_unreadable_legacy_state_blocks_the_install(self) -> None:
        # Review j#102180 finding 1. The previous fix verified "not loaded" through a probe that
        # collapsed EVERY non-zero read into that answer, so a permission-denied read passed as a
        # verified stop and the plist was deleted on the strength of a read that never happened.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = _LegacyUnreadable()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        # Nothing was removed and nothing was added: an unreadable state cannot authorize either.
        self.assertTrue(legacy.exists())
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertNotIn("bootstrap", runner.verbs)

    def test_unreadable_is_a_distinct_answer_from_still_loaded(self) -> None:
        # Two different facts — "it is running" and "I cannot tell" — must not share a token, or the
        # operator cannot tell which one they are looking at.
        self.assertNotEqual(
            sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE, sl.REASON_LEGACY_DRAIN_STILL_LOADED
        )
        self.assertEqual(
            {sl.PROBE_LOADED, sl.PROBE_CONFIRMED_ABSENT, sl.PROBE_UNREADABLE},
            {"loaded", "confirmed_absent", "unreadable"},
        )

    def test_absence_needs_the_whole_conjunction_not_any_one_signal(self) -> None:
        # Review j#102200 finding r3f1. Deleting a registration on the strength of an ERROR needs
        # evidence specific enough to act on, so absence requires ALL of: a not-found exit code, a
        # recognized not-found phrase, our own label named in it, and no signal that the read failed
        # for another reason. Any one of those alone is too weak — `113` + `Operation not permitted`
        # is a PERMISSION failure, and reading it as absence is what deleted an owned plist.
        owned = sl.SUPERVISOR_AGENT.label
        not_found = f'Could not find service "{owned}" in domain for gui'
        cases = [
            (_result(0, "\tpid = 5\n"), sl.PROBE_LOADED, "zero exit is loaded"),
            (_result(113, stderr=not_found), sl.PROBE_CONFIRMED_ABSENT, "full conjunction"),
            (_result(113), sl.PROBE_UNREADABLE, "code alone is not enough"),
            (_result(1, stderr=not_found), sl.PROBE_UNREADABLE, "phrase alone is not enough"),
            (
                _result(113, stderr="Operation not permitted"),
                sl.PROBE_UNREADABLE,
                "the r3f1 reproduction: code matches but the read was denied",
            ),
            (
                _result(113, stderr=f"{not_found}; Operation not permitted"),
                sl.PROBE_UNREADABLE,
                "a denial signal disqualifies even alongside a not-found phrase",
            ),
            (
                _result(113, stderr='Could not find service "com.example.other" in domain'),
                sl.PROBE_UNREADABLE,
                "not-found about SOMEONE ELSE says nothing about ours",
            ),
            (_result(1), sl.PROBE_UNREADABLE, "a bare non-zero says nothing"),
        ]
        for scripted, expected, why in cases:
            state = sl._probe(FakeRunner(print_result=scripted))["state"]
            self.assertEqual(state, expected, why)

    def test_no_character_class_assumption_admits_a_different_label(self) -> None:
        # Review j#102309 finding r5f1. The boundary used to be inferred from a character allowlist
        # invented here (alphanumerics plus `.-_`), but Apple documents `Label` only as a unique
        # identifying string and constrains its characters nowhere. Every separator outside that
        # invented set therefore read as a boundary, so these DIFFERENT labels all matched ours —
        # and the match could authorize unlinking our registration.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for suffix in ("@helper", ":helper", "+helper", "/helper", " helper",
                       ".helper", "-secondary", "\u00e9helper", "\u3042"):
            other = f"{owned}{suffix}"
            rendered = f'Could not find service "{other}" in domain for gui'
            self.assertFalse(
                sl._names_exactly(rendered, owned),
                f"a not-found about {other!r} is not about {owned!r}",
            )
            state = sl._probe(FakeRunner(print_result=_result(113, stderr=rendered)))["state"]
            self.assertEqual(state, sl.PROBE_UNREADABLE, suffix)

    def test_only_the_quoted_form_binds_the_reading_to_our_label(self) -> None:
        # Quotes make the boundary OBSERVED rather than assumed. An unquoted mention cannot prove
        # where the name ends, so it yields no binding — deliberately an over-refusal until the real
        # launchctl wording is captured on a live host (#15194).
        owned = sl.LEGACY_DRAIN_AGENT.label
        self.assertTrue(
            sl._names_exactly(f'Could not find service "{owned}" in domain for gui', owned)
        )
        self.assertFalse(
            sl._names_exactly(f"Could not find service {owned} in domain for gui", owned)
        )

    def test_a_label_differing_only_in_case_is_a_different_label(self) -> None:
        # Review j#102327 finding r6f1. The message and the queried target were both lower-cased
        # before the quoted comparison, so a not-found naming `ORG.MOZYO-BRIDGE...DRAIN` — a
        # different byte sequence, and therefore a job this adapter never installed — read as a
        # confirmed absence of OURS and could authorize unlinking our plist. Apple documents `Label`
        # as a string that uniquely identifies a job and says nothing about case-insensitive
        # matching, so folding case here was an assumption, not a contract.
        owned = sl.LEGACY_DRAIN_AGENT.label
        target = sl._service_target(sl.LEGACY_DRAIN_AGENT)
        variants = (
            (owned.upper(), "the bare label upper-cased"),
            (owned.title(), "the bare label title-cased"),
            (owned[:-1] + owned[-1].upper(), "a single character of the bare label"),
            (target.upper(), "the full target upper-cased"),
        )
        for other, why in variants:
            rendered = f'Could not find service "{other}" in domain for gui'
            self.assertFalse(sl._names_exactly(rendered, owned), why)
            self.assertFalse(sl._names_exactly(rendered, target), why)
            state = sl._probe(
                FakeRunner(print_result=_result(113, stderr=rendered)),
                agent=sl.LEGACY_DRAIN_AGENT,
            )["state"]
            self.assertEqual(state, sl.PROBE_UNREADABLE, why)

    def test_quoting_it_cannot_parse_is_unreadable_not_a_match(self) -> None:
        # Review j#102378 finding r7f1. `f'"{token}"' in message` was a substring test, not a parse:
        # for a different label rendered with a backslash escape, the opening quote of the "match"
        # is that label's own escaped quote and the closing one is the outer delimiter, so the two
        # quotes bounding the hit are not the two ends of one span. launchctl's quoting grammar is
        # unverified, so every sign of a grammar this scanner cannot read must refuse.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for rendered, why in (
            (f'Could not find service "prefix\\"{owned}" in domain', "backslash-escaped quote"),
            (f'Could not find service "prefix""{owned}" in domain', 'doubled-quote escaping'),
            (f'Could not find service "prefix"{owned}" in domain', "an unbalanced quote run"),
            (f'Could not find service "{owned}\\" in domain', "a trailing escape in our own span"),
        ):
            self.assertFalse(sl._names_exactly(rendered, owned), why)
            state = sl._probe(
                FakeRunner(print_result=_result(113, stderr=rendered)),
                agent=sl.LEGACY_DRAIN_AGENT,
            )["state"]
            self.assertEqual(state, sl.PROBE_UNREADABLE, why)

    def test_a_readable_message_with_two_quoted_names_still_binds(self) -> None:
        # The complement: refusing ambiguity must not refuse a message that simply quotes more than
        # one thing. Both spans are complete and unambiguous, and one of them is ours.
        owned = sl.LEGACY_DRAIN_AGENT.label
        state = sl._probe(
            FakeRunner(
                print_result=_result(
                    113, stderr=f'Could not find service "{owned}" in domain "gui/501"'
                )
            ),
            agent=sl.LEGACY_DRAIN_AGENT,
        )["state"]
        self.assertEqual(state, sl.PROBE_CONFIRMED_ABSENT)

    def test_an_escaped_quote_label_keeps_the_plist_end_to_end(self) -> None:
        # The consequence, through the destructive path: an unparseable message must leave the
        # retired registration on disk rather than authorize unlinking it.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        rendered = (
            f'Could not find service "prefix\\"{sl.LEGACY_DRAIN_AGENT.label}" in domain for gui'
        )

        class _EscapedQuoteLabelNotFound:
            def __call__(self, argv):
                argv = list(argv)
                target = argv[2] if len(argv) > 2 else ""
                if sl.LEGACY_DRAIN_AGENT.label in target:
                    return _result(113 if argv[1] == "print" else 1, stderr=rendered)
                return _result(0)

        result = sl.remove_legacy_drain(os_home=self.os_home, runner=_EscapedQuoteLabelNotFound())
        self.assertFalse(result["removed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())

    def test_the_wording_stays_case_insensitive_while_the_identity_does_not(self) -> None:
        # The two readings of one string are deliberately different. launchctl's prose is not an
        # API, so its capitalization cannot be a contract — but the label it names is an identity,
        # and identities are compared as bytes. A shouted phrase around OUR exact label still reads
        # as a confirmed absence.
        owned = sl.LEGACY_DRAIN_AGENT.label
        state = sl._probe(
            FakeRunner(
                print_result=_result(113, stderr=f'COULD NOT FIND SERVICE "{owned}" IN DOMAIN')
            ),
            agent=sl.LEGACY_DRAIN_AGENT,
        )["state"]
        self.assertEqual(state, sl.PROBE_CONFIRMED_ABSENT)

    def test_a_case_folded_label_not_found_keeps_the_plist_end_to_end(self) -> None:
        # The consequence the two layers above exist to prevent, exercised through the destructive
        # path: an unreadable state must leave the retired registration on disk.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        other = sl.LEGACY_DRAIN_AGENT.label.upper()

        class _CaseFoldedLabelNotFound:
            def __call__(self, argv):
                argv = list(argv)
                target = argv[2] if len(argv) > 2 else ""
                if sl.LEGACY_DRAIN_AGENT.label in target:
                    code = 113 if argv[1] == "print" else 1
                    return _result(
                        code, stderr=f'Could not find service "{other}" in domain for gui'
                    )
                return _result(0)

        result = sl.remove_legacy_drain(os_home=self.os_home, runner=_CaseFoldedLabelNotFound())
        self.assertFalse(result["removed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())

    def test_a_not_found_about_a_LONGER_label_is_not_about_ours(self) -> None:
        # Review j#102235 finding r4f1. `label in message` is a substring test, and our label is a
        # PREFIX of any longer one, so a not-found about `<label>.helper` satisfied the check for
        # `<label>` — and deleted its plist while that agent may still have been running.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for other, why in (
            (f"{owned}.helper", "suffix: ours is a prefix of theirs"),
            (f"{owned}-secondary", "suffix via a label-continuation character"),
            ("com.example.other", "an unrelated label"),
        ):
            for rendered, form in (
                (f'Could not find service "{other}" in domain for gui', "quoted"),
                (f"Could not find service {other} in domain for gui", "bare"),
            ):
                state = sl._probe(FakeRunner(print_result=_result(113, stderr=rendered)))["state"]
                self.assertEqual(state, sl.PROBE_UNREADABLE, f"{why} ({form})")

    def test_a_quoted_not_found_about_our_own_label_still_counts(self) -> None:
        # The complement: the fence must not have become so tight that a genuine, quoted absence is
        # refused. Only the quoted form qualifies since j#102309 r5f1 — the unquoted variants that
        # used to pass here are now `unreadable`, because an unquoted mention cannot prove where the
        # name ends. That is a deliberate over-refusal, pinned by the sibling test above.
        owned = sl.LEGACY_DRAIN_AGENT.label
        state = sl._probe(
            FakeRunner(
                print_result=_result(
                    113, stderr=f'Could not find service "{owned}" in domain for gui'
                )
            ),
            agent=sl.LEGACY_DRAIN_AGENT,
        )["state"]
        self.assertEqual(state, sl.PROBE_CONFIRMED_ABSENT)

    def test_a_longer_label_not_found_keeps_the_plist_end_to_end(self) -> None:
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        other = f"{sl.LEGACY_DRAIN_AGENT.label}.helper"

        class _OtherLabelNotFound:
            def __call__(self, argv):
                argv = list(argv)
                target = argv[2] if len(argv) > 2 else ""
                if sl.LEGACY_DRAIN_AGENT.label in target:
                    code = 113 if argv[1] == "print" else 1
                    return _result(
                        code, stderr=f'Could not find service "{other}" in domain for gui'
                    )
                return _result(0)

        result = sl.remove_legacy_drain(os_home=self.os_home, runner=_OtherLabelNotFound())
        self.assertFalse(result["removed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())

    def test_the_r3f1_reproduction_keeps_the_plist_and_refuses(self) -> None:
        # End-to-end shape of the same defect: exit 113 with a permission error must not remove the
        # owned retired plist.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)

        class _Code113ButDenied:
            def __call__(self, argv):
                argv = list(argv)
                target = argv[2] if len(argv) > 2 else ""
                if sl.LEGACY_DRAIN_AGENT.label in target:
                    code = 113 if argv[1] == "print" else 1
                    return _result(code, stderr="Operation not permitted")
                return _result(0)

        result = sl.remove_legacy_drain(os_home=self.os_home, runner=_Code113ButDenied())
        self.assertFalse(result["removed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())

    def test_an_absent_launchctl_is_unreadable_not_absent(self) -> None:
        # No launchctl means no answer about the job — emphatically not "the job is gone".
        def _no_launchctl(argv):
            raise FileNotFoundError("launchctl")

        self.assertEqual(sl._probe(_no_launchctl)["state"], sl.PROBE_UNREADABLE)
        self.assertFalse(sl._probe(_no_launchctl)["loaded"])

    def test_a_successful_bootout_is_positive_evidence_without_reading_an_error(self) -> None:
        # The common clean path must not depend on interpreting launchctl's error taxonomy at all:
        # a bootout that EXITS ZERO means we just unloaded it ourselves.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = FakeRunner()  # every command succeeds, including the legacy bootout
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"], result)
        self.assertTrue(result["legacy_drain_removed"])
        self.assertFalse(legacy.exists())
        # No `print` of the retired label was needed to reach that conclusion.
        self.assertNotIn(
            f"{_GUI_DOMAIN}/{sl.LEGACY_DRAIN_AGENT.label}",
            [c[2] for c in runner.calls if len(c) >= 3 and c[1] == "print"],
        )

    def test_a_legacy_agent_that_was_never_loaded_migrates_cleanly(self) -> None:
        # The complement, and the reason the bootout RETURN CODE is not the test: `launchctl bootout`
        # exits non-zero for a label that was never loaded, which is the ordinary state of an already
        # stopped retired agent. Gating on the return code would refuse every clean migration.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = _LegacyNeverLoaded()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertTrue(result["performed"], result)
        self.assertTrue(result["legacy_drain_removed"])
        self.assertFalse(legacy.exists())
        self.assertTrue(sl.plist_path(self.os_home).exists())

    def test_an_unlinkable_legacy_plist_blocks_the_install_after_a_verified_stop(self) -> None:
        _write_home_credential(self.mozyo_home)
        _legacy_drain_plist(self.os_home)
        runner = _LegacyNeverLoaded()
        with patch.object(sl.Path, "unlink", side_effect=OSError("read-only")):
            result = sl.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=runner, which=_which_found,
            )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_REMOVAL_FAILED)
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertNotIn("bootstrap", runner.verbs)

    def test_migration_is_darwin_only_and_status_reports_a_pending_one(self) -> None:
        _write_home_credential(self.mozyo_home)
        _legacy_drain_plist(self.os_home)
        with patch.object(sl, "_running_on_darwin", return_value=False):
            result = sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                                runner=FakeRunner(), which=_which_found)
        self.assertEqual(result["reason"], sl.REASON_UNSUPPORTED_PLATFORM)
        self.assertTrue(sl.plist_path(self.os_home, agent=sl.LEGACY_DRAIN_AGENT).exists())
        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(), which=_which_found,
        )
        self.assertEqual(status["legacy_drain"], sl.LEGACY_DRAIN_OWNED)


class CommonStatusContractTest(_DarwinCase):
    """Redmine #15192: 実行内容 / 次回起動 / 直近の終了結果 mean the same thing on both hosts."""

    def _installed_status(self, *, print_result=None) -> dict:
        _write_home_credential(self.mozyo_home)
        sl.install(os_home=self.os_home, mozyo_home=self.mozyo_home,
                   runner=FakeRunner(), which=_which_found)
        return sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=print_result), which=_which_found,
        )

    def test_status_publishes_the_shared_contract_keys(self) -> None:
        status = self._installed_status()
        for key in (
            "installed", "loaded", "pid", "scheduled_interval_seconds", "home_pin",
            "executable_matches", "keep_alive_present", "no_environment_block",
            "credential_readiness", "installed_command", "next_elapse", "next_elapse_basis",
            "last_result", "last_exit_status", "last_exit_at",
            "provider_reconcile_interval_seconds",
        ):
            self.assertIn(key, status, key)

    def test_installed_command_is_the_exact_argv_and_carries_no_secret(self) -> None:
        status = self._installed_status()
        self.assertEqual(status["installed_command"][-4:-2], ["supervisor", "--run-once"])
        self.assertNotIn("home-key-sentinel", " ".join(status["installed_command"]))

    def test_next_elapse_is_an_explicit_unknown_not_an_absent_key(self) -> None:
        # launchd publishes no next-fire time for a StartInterval agent. An ABSENT key would read as
        # "nothing scheduled" while the agent IS scheduled, so the key is present and says unknown.
        status = self._installed_status()
        self.assertEqual(status["next_elapse"], "")
        self.assertEqual(status["next_elapse_basis"], sl.NEXT_ELAPSE_UNKNOWN)

    def test_the_unknown_basis_token_matches_the_systemd_adapter(self) -> None:
        # A drift guard instead of a cross-OS import: neither adapter imports the other.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_systemd as ss,
        )

        self.assertEqual(sl.NEXT_ELAPSE_UNKNOWN, ss.NEXT_ELAPSE_UNKNOWN)

    def test_last_result_uses_the_shared_vocabulary(self) -> None:
        clean = self._installed_status(print_result=_result(0, "\tpid = 4321\n\tlast exit code = 0\n"))
        self.assertEqual(clean["last_result"], sl.LAST_RESULT_SUCCESS)
        self.assertEqual(clean["last_exit_status"], 0)
        failed = self._installed_status(
            print_result=_result(0, "\tpid = 4321\n\tlast exit status = 2\n")
        )
        self.assertEqual(failed["last_result"], sl.LAST_RESULT_EXIT_CODE)
        self.assertEqual(failed["last_exit_status"], 2)

    def test_an_unreadable_last_exit_is_unknown_not_a_crash(self) -> None:
        # Same defect class as the #14753 pid read: a non-ASCII digit must not raise out of a
        # projection whose whole contract is "never raises".
        status = self._installed_status(
            print_result=_result(0, "\tpid = 4321\n\tlast exit code = ²\n")
        )
        self.assertIsNone(status["last_exit_status"])
        self.assertEqual(status["last_result"], "")

    def test_status_reports_no_last_result_when_the_service_is_unknown_to_launchd(self) -> None:
        status = self._installed_status(print_result=_result(113))  # non-zero -> not loaded
        self.assertFalse(status["loaded"])
        self.assertIsNone(status["last_exit_status"])
        self.assertEqual(status["last_result"], "")


if __name__ == "__main__":
    unittest.main()
