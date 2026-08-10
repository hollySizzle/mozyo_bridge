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

import errno
import inspect
import json
import os
import plistlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_launchd as sl,
    supervisor_launchd_agent as agent,
    supervisor_launchd_fs as fs,
    supervisor_launchd_process as process,
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


def _write_plist(target: Path, label: str) -> Path:
    """Write a parseable plist carrying exactly ``label`` — the identity every verb classifies on."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/opt/bin/x"]}))
    return target


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
        p_uid = patch.object(os, "getuid", return_value=_TEST_UID, create=True)
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


class LaunchdModuleBoundaryTest(unittest.TestCase):
    """The text module cannot directly read an owned plist or start a process (r15f4)."""

    def test_process_and_filesystem_effects_live_in_their_named_modules(self) -> None:
        agent_source = inspect.getsource(agent)
        self.assertNotIn("import subprocess", agent_source)
        self.assertNotIn("def default_runner", agent_source)
        self.assertNotIn("def launchctl", agent_source)
        self.assertNotIn(".read_bytes(", agent_source)
        self.assertIs(sl._default_runner, process.default_runner)
        self.assertIs(sl._launchctl, process.launchctl)
        self.assertEqual(process.default_runner.__module__, process.__name__)
        self.assertEqual(fs.read_owned.__module__, fs.__name__)


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

    def test_install_refuses_command_drift_during_bootout_before_bootstrap(self) -> None:
        target = sl.plist_path(self.os_home)
        calls = []

        def runner(argv):
            argv = list(argv)
            calls.append(argv)
            if argv[1] == "bootout":
                target.write_bytes(sl.render_plist(
                    [
                        "/tmp/unapproved",
                        "workflow",
                        "supervisor",
                        "--run-once",
                        "--home",
                        _resolved(self.mozyo_home),
                    ],
                    interval_seconds=sl.DEFAULT_OS_TICK_INTERVAL_SECONDS,
                    os_home=self.os_home,
                ))
            return _result()

        result = sl.install(
            os_home=self.os_home,
            mozyo_home=self.mozyo_home,
            runner=runner,
            which=_which_found,
        )

        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_INSTALLED_COMMAND_DRIFT, result["reason"])
        self.assertNotIn("bootstrap", [call[1] for call in calls])


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

    def test_restart_refuses_command_drift_during_print_before_kickstart(self) -> None:
        self._install_ready()
        target = sl.plist_path(self.os_home)
        calls = []

        def runner(argv):
            argv = list(argv)
            calls.append(argv)
            if argv[1] == "print":
                target.write_bytes(sl.render_plist(
                    [
                        "/tmp/unapproved",
                        "workflow",
                        "supervisor",
                        "--run-once",
                        "--home",
                        _resolved(self.mozyo_home),
                    ],
                    interval_seconds=sl.DEFAULT_OS_TICK_INTERVAL_SECONDS,
                    os_home=self.os_home,
                ))
                return _result(0, stdout="state = running\n\tpid = 4242\n")
            return _result()

        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)

        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_INSTALLED_COMMAND_DRIFT, result["reason"])
        self.assertNotIn("kickstart", [call[1] for call in calls])

    def test_restart_refuses_zero_mutation_on_a_confirmed_absence(self) -> None:
        # The fixture now carries a real confirmed absence — a recognized clause whose operand IS our
        # label. The old one said only `"not found"`, which binds to nothing and is therefore an
        # UNREADABLE read, not an absent one; the sibling test below covers that case explicitly.
        self._install_ready()
        owned = sl.SUPERVISOR_AGENT.label
        runner = FakeRunner(
            print_result=_result(113, stderr=f'Could not find service "{owned}" in domain for gui')
        )
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_SERVICE_NOT_LOADED, result["reason"])
        self.assertEqual(sl.PROBE_CONFIRMED_ABSENT, result["probe_state"])
        self.assertNotIn("kickstart", runner.verbs)

    def test_restart_separates_an_unreadable_state_from_a_confirmed_absence(self) -> None:
        # Review j#102398 finding r9f2. `_is_loaded` collapsed the three-valued probe to a bool, so a
        # permission-denied read and a confirmed not-found came back as the same
        # `service_not_loaded` with no `probe_state` — reporting "I cannot tell" as an established
        # fact, and diverging from the Linux adapter under a backend that declares the verbs
        # identical. Both still refuse with zero mutation; they no longer claim the same thing.
        self._install_ready()
        runner = FakeRunner(print_result=_result(1, stderr="Operation not permitted"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_SERVICE_STATE_UNREADABLE, result["reason"])
        self.assertEqual(sl.PROBE_UNREADABLE, result["probe_state"])
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
        # R4-F3: a present-but-unparseable plist is unhealthy, not "not installed". That distinction
        # is what this test exists for and it still holds; since j#102550 r13f1 routed restart
        # through the shared classifier, the REASON is the accurate `plist_unreadable` — a file we
        # cannot parse is unidentifiable before it is un-pinnable — while `home_pin` keeps the value
        # it always reported, so consumers of that field see no change.
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x01 not a plist")
        runner = FakeRunner(print_result=_result(0, stdout="pid = 9\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
        self.assertEqual(sl.HOME_PIN_UNREADABLE, result["home_pin"])
        self.assertNotEqual(sl.REASON_NOT_INSTALLED, result["reason"])  # still not "absent"
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
        # The plist must carry OUR Label to be removable. This test used to write `b"placeholder"` —
        # a file that parses as nothing — and assert its deletion, so a test named "exactly owned"
        # pinned the deletion of a file whose owner was unknowable (review j#102496 r12f2).
        plist_file = _write_plist(sl.plist_path(self.os_home), sl.SUPERVISOR_LAUNCHD_LABEL)
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

    def test_drifted_owned_argv_is_absent_from_repr_and_json(self) -> None:
        sentinel = "LAUNCHD-DRIFT-SECRET-SENTINEL"
        _write_home_credential(self.mozyo_home)
        argv = [
            "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
            "--home", _resolved(self.mozyo_home), "--token", sentinel,
        ]
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            sl.render_plist(
                argv, interval_seconds=sl.DEFAULT_OS_TICK_INTERVAL_SECONDS,
                os_home=self.os_home,
            )
        )

        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=_result(113)), which=_which_found,
        )
        self.assertFalse(status["executable_matches"])
        self.assertEqual([], status["installed_command"])
        self.assertNotIn(sentinel, repr(status))
        self.assertNotIn(sentinel, json.dumps(status))


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


class _LegacyBootoutSucceeds:
    """launchctl where booting the RETIRED label out SUCCEEDS — the only authority to unlink.

    Modelled explicitly on the action rather than on wording: since the safe interim invariant
    (owner delegation j#102452 / gateway disposition j#102458), a non-zero bootout ends the decision
    and no message is read, so a fake that returns not-found prose no longer describes a migration.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
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
        runner = _LegacyBootoutSucceeds()
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

        class _MigrationOkBootstrapFails(_LegacyBootoutSucceeds):
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
        result = sl.uninstall(os_home=self.os_home, runner=_LegacyBootoutSucceeds())
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

    def test_any_nonzero_bootout_blocks_the_install_without_reading_the_message(self) -> None:
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
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
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

    def test_the_probe_vocabulary_is_still_three_valued(self) -> None:
        # The status projection keeps all three answers; only the DESTRUCTIVE path stopped consuming
        # them. A retired-agent refusal can no longer claim "it is running" — that fact was only ever
        # derivable by reading the message, which no longer authorizes anything — so the migration
        # reports the honest `state_unreadable` instead of a distinction it cannot establish.
        self.assertEqual(
            {sl.PROBE_LOADED, sl.PROBE_CONFIRMED_ABSENT, sl.PROBE_UNREADABLE},
            {"loaded", "confirmed_absent", "unreadable"},
        )
        self.assertFalse(hasattr(sl, "REASON_LEGACY_DRAIN_STILL_LOADED"))

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

    def test_two_quoted_names_bind_when_OURS_is_the_clause_operand(self) -> None:
        # The complement, restated correctly (review j#102383 finding r8f1). The old comment here
        # said "one of them is ours", generalising a rule that is false: what matters is not that our
        # label appears among the spans, but that it is the span the not-found clause is ABOUT. This
        # message qualifies because ours directly follows the wording; the domain is the second span.
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

    def test_a_not_found_about_ANOTHER_service_does_not_bind_via_a_second_span(self) -> None:
        # The negative half the old test's comment wrongly excluded. The clause explicitly reports a
        # DIFFERENT service as missing; our label is an unrelated second span. Containing our name
        # and being about our service are different claims, and only the second may authorize a
        # deletion.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for rendered, why in (
            (f'Could not find service "com.example.other"; suggestion "{owned}"',
             "ours is a trailing suggestion"),
            (f'"{owned}" Could not find service "com.example.other"',
             "ours precedes the clause"),
            (f'Could not find service "com.example.other". Could not find service "{owned}"',
             "two clauses: no rule says which governs"),
        ):
            state = sl._probe(
                FakeRunner(print_result=_result(113, stderr=rendered)),
                agent=sl.LEGACY_DRAIN_AGENT,
            )["state"]
            self.assertEqual(state, sl.PROBE_UNREADABLE, why)

    def test_a_clause_and_an_operand_in_DIFFERENT_streams_do_not_bind(self) -> None:
        # Review j#102417 finding r10f1. `stderr` and `stdout` were concatenated into one string
        # before parsing, so the joining newline satisfied "clause and operand separated by
        # whitespace only" — a sentence launchctl never wrote. Hardening the parser is worth nothing
        # if its caller can manufacture the adjacency the parser checks. Both directions are pinned:
        # the reverse only failed before by accident of concatenation order, which a later change to
        # that order would silently undo.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for stderr, stdout, why in (
            ("Could not find service", f'"{owned}"', "phrase in stderr, operand in stdout"),
            (f'"{owned}"', "Could not find service", "phrase in stdout, operand in stderr"),
        ):
            self.assertFalse(
                sl._says_not_found(
                    _result(113, stdout=stdout, stderr=stderr),
                    f"{_GUI_DOMAIN}/{owned}",
                ),
                why,
            )

    def test_a_canonical_clause_binds_from_either_stream_alone(self) -> None:
        # The complement: a whole clause living in one stream is exactly what this recognizes, and
        # launchctl may write it to either.
        owned = sl.LEGACY_DRAIN_AGENT.label
        clause = f'Could not find service "{owned}" in domain for gui'
        for stderr, stdout, why in (
            (clause, "", "stderr alone"),
            ("", clause, "stdout alone"),
            (clause, clause, "both streams agree"),
        ):
            self.assertTrue(
                sl._says_not_found(
                    _result(113, stdout=stdout, stderr=stderr),
                    f"{_GUI_DOMAIN}/{owned}",
                ),
                why,
            )

    def test_streams_that_disagree_or_dangle_are_unreadable(self) -> None:
        # One stream's positive reading may not paper over the other's contradiction or ambiguity.
        owned = sl.LEGACY_DRAIN_AGENT.label
        clause = f'Could not find service "{owned}" in domain for gui'
        for stderr, stdout, why in (
            (clause, 'Could not find service "com.example.other"', "streams name different services"),
            (clause, "Could not find service", "the other stream has wording but no operand"),
            (clause, "Operation not permitted", "a denial signal anywhere disqualifies the read"),
        ):
            self.assertFalse(
                sl._says_not_found(
                    _result(113, stdout=stdout, stderr=stderr),
                    f"{_GUI_DOMAIN}/{owned}",
                ),
                why,
            )

    def test_cross_stream_vectors_keep_the_plist_end_to_end(self) -> None:
        owned = sl.LEGACY_DRAIN_AGENT.label
        for stderr, stdout, why in (
            ("Could not find service", f'"{owned}"', "phrase in stderr, operand in stdout"),
            (f'"{owned}"', "Could not find service", "phrase in stdout, operand in stderr"),
        ):
            legacy = _legacy_drain_plist(self.os_home)

            class _CrossStream:
                def __call__(self, argv):
                    argv = list(argv)
                    target = argv[2] if len(argv) > 2 else ""
                    if owned in target:
                        if argv[1] == "print":
                            return _result(113, stdout=stdout, stderr=stderr)
                        return _result(1, stderr="bootout failed")
                    return _result(0)

            result = sl.remove_legacy_drain(os_home=self.os_home, runner=_CrossStream())
            self.assertFalse(result["removed"], why)
            self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE, why)
            self.assertTrue(legacy.exists(), why)

    def test_the_clause_must_be_positionally_bound_to_its_operand(self) -> None:
        # Review j#102398 finding r9f1. The parser searched for the phrase and for a quote
        # independently and called the pair a clause. Three messages satisfied that while saying
        # something else, and each authorized unlinking the owned registration.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for rendered, why in (
            (f'Could not find service com.example.other; suggestion "{owned}"',
             "the clause's real operand is UNQUOTED; ours is a later span"),
            (f'diagnostic "could not find service"{owned}"x" "{owned}"',
             "the phrase is INSIDE a span, so the next quote is a closing delimiter"),
            (f'no such processnot find service "{owned}"',
             "two abutting phrases are two clauses, not one"),
            (f'Could not find service oops "{owned}"',
             "prose between the clause and the span"),
        ):
            state = sl._probe(
                FakeRunner(print_result=_result(113, stderr=rendered)),
                agent=sl.LEGACY_DRAIN_AGENT,
            )["state"]
            self.assertEqual(state, sl.PROBE_UNREADABLE, why)

    def test_the_canonical_form_still_binds_after_positional_tightening(self) -> None:
        # The complement: tightening must not refuse the shape this is supposed to recognize.
        owned = sl.LEGACY_DRAIN_AGENT.label
        for rendered, why in (
            (f'Could not find service "{owned}" in domain for gui', "canonical"),
            (f'COULD NOT FIND SERVICE "{owned}"', "wording is prose: case-insensitive"),
            (f'Could not find service    "{owned}"', "whitespace only between clause and operand"),
        ):
            state = sl._probe(
                FakeRunner(print_result=_result(113, stderr=rendered)),
                agent=sl.LEGACY_DRAIN_AGENT,
            )["state"]
            self.assertEqual(state, sl.PROBE_CONFIRMED_ABSENT, why)

    def test_case_folding_never_shifts_the_offsets_it_reports(self) -> None:
        # Offsets are computed on the ORIGINAL message. Folding the whole string first and indexing
        # the result misaligns wherever folding changes length — `len("İ".lower()) == 2` — so a
        # message carrying such a character would slice the original at the wrong place.
        self.assertNotEqual(len("\u0130"), len("\u0130".lower()))
        owned = sl.LEGACY_DRAIN_AGENT.label
        rendered = f'\u0130 Could not find service "{owned}" in domain'
        state = sl._probe(
            FakeRunner(print_result=_result(113, stderr=rendered)),
            agent=sl.LEGACY_DRAIN_AGENT,
        )["state"]
        self.assertEqual(state, sl.PROBE_CONFIRMED_ABSENT)

    def test_the_r9f1_vectors_keep_the_plist_end_to_end(self) -> None:
        owned = sl.LEGACY_DRAIN_AGENT.label
        for rendered in (
            f'Could not find service com.example.other; suggestion "{owned}"',
            f'diagnostic "could not find service"{owned}"x" "{owned}"',
            f'no such processnot find service "{owned}"',
        ):
            legacy = _legacy_drain_plist(self.os_home)

            class _Vector:
                def __call__(self, argv):
                    argv = list(argv)
                    target = argv[2] if len(argv) > 2 else ""
                    if owned in target:
                        return _result(113 if argv[1] == "print" else 1, stderr=rendered)
                    return _result(0)

            result = sl.remove_legacy_drain(os_home=self.os_home, runner=_Vector())
            self.assertFalse(result["removed"], rendered)
            self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE, rendered)
            self.assertTrue(legacy.exists(), rendered)

    def test_a_not_found_about_another_service_keeps_the_plist_end_to_end(self) -> None:
        # The consequence the finding turned on: this reading reached `remove_legacy_drain` and
        # unlinked the owned plist.
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        owned = sl.LEGACY_DRAIN_AGENT.label
        rendered = f'Could not find service "com.example.other"; suggestion "{owned}"'

        class _NotFoundAboutAnotherService:
            def __call__(self, argv):
                argv = list(argv)
                target = argv[2] if len(argv) > 2 else ""
                if owned in target:
                    return _result(113 if argv[1] == "print" else 1, stderr=rendered)
                return _result(0)

        result = sl.remove_legacy_drain(
            os_home=self.os_home, runner=_NotFoundAboutAnotherService()
        )
        self.assertFalse(result["removed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())

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
        runner = _LegacyBootoutSucceeds()
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
        runner = _LegacyBootoutSucceeds()
        with patch.object(os, "unlink", side_effect=OSError("read-only")):
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


class NonzeroBootoutNeverAuthorizesUnlinkTest(_DarwinCase):
    """The safe interim invariant (owner delegation j#102452 / gateway disposition j#102458).

    Six rounds tried to make launchctl's error text safe to interpret — an exit code taken as a
    contract, a substring match, an invented character class, an open negation, a phrase never bound
    to its operand, a position rule forgeable across two streams, an unparseable stream read as
    silence, a newline read as a space. Every fix was locally right and rested on an unverified
    premise about output nobody here has observed. The defect was never one premise: it was that a
    destructive action depended on parsing an undocumented grammar at all.

    So the authority is now an ACTION, not a reading: `launchctl bootout` returning 0 means this
    process just unloaded that job. Anything else keeps the plist. These pin that the message is not
    merely *insufficient* but *unread* — `print` is never even invoked.
    """

    #: Every wording vector any prior round turned into a deletion, plus the shapes r11 found last.
    def _vectors(self) -> list:
        owned = sl.LEGACY_DRAIN_AGENT.label
        canonical = f'Could not find service "{owned}" in domain for gui'
        return [
            (_result(113, stderr=canonical), "canonical owned not-found"),
            (_result(113, stderr=f'Could not find service "x\\'), "malformed / backslash"),
            (_result(113, stderr=f'Could not find service "{owned}'), "unbalanced quote"),
            (_result(113, stdout=f'"{owned}"', stderr="Could not find service"), "cross-stream"),
            (_result(113, stderr=f'Could not find service\n"{owned}"'), "LF between phrase and label"),
            (_result(113, stderr=f'Could not find service\r\n"{owned}"'), "CRLF"),
            (_result(113, stderr=f'Could not find service\n\n"{owned}"'), "multiple lines"),
            (
                _result(113, stderr='Could not find service "com.example.other"'),
                "a different label",
            ),
            (_result(113, stderr="Operation not permitted"), "denial"),
            (_result(0, stdout="\tstate = running\n"), "print says LOADED"),
        ]

    def test_no_wording_survives_a_nonzero_bootout(self) -> None:
        for print_result, why in self._vectors():
            legacy = _legacy_drain_plist(self.os_home)
            runner = _BootoutFails(print_result)
            result = sl.remove_legacy_drain(os_home=self.os_home, runner=runner)
            self.assertFalse(result["removed"], why)
            self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE, why)
            self.assertTrue(legacy.exists(), why)

    def test_the_message_is_not_read_at_all_after_a_nonzero_bootout(self) -> None:
        # Stronger than "the wording did not convince it": the wording is never consulted, so no
        # future parser change can reopen this path.
        _legacy_drain_plist(self.os_home)
        runner = _BootoutFails(_result(113, stderr="Could not find service"))
        sl.remove_legacy_drain(os_home=self.os_home, runner=runner)
        self.assertNotIn("print", runner.verbs)

    def test_install_refuses_and_never_bootstraps_after_a_nonzero_bootout(self) -> None:
        _write_home_credential(self.mozyo_home)
        legacy = _legacy_drain_plist(self.os_home)
        runner = _BootoutFails(_result(113, stderr="Could not find service"))
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE)
        self.assertTrue(legacy.exists())
        self.assertFalse(sl.plist_path(self.os_home).exists())
        self.assertNotIn("bootstrap", runner.verbs)

    def test_a_succeeding_bootout_still_completes_the_owned_cleanup(self) -> None:
        # The invariant closes a path; it must not close the migration itself.
        legacy = _legacy_drain_plist(self.os_home)
        result = sl.remove_legacy_drain(os_home=self.os_home, runner=_LegacyBootoutSucceeds())
        self.assertTrue(result["removed"])
        self.assertEqual(result["reason"], "")
        self.assertFalse(legacy.exists())

    def test_the_foreign_and_unreadable_preflights_are_unchanged(self) -> None:
        # These refuse BEFORE any bootout, on identity, and the invariant must not disturb them.
        foreign = _legacy_drain_plist(self.os_home, label="com.example.someone-else")
        runner = _LegacyBootoutSucceeds()
        result = sl.remove_legacy_drain(os_home=self.os_home, runner=runner)
        self.assertEqual(result["reason"], sl.REASON_LEGACY_DRAIN_FOREIGN_LABEL)
        self.assertTrue(foreign.exists())
        self.assertEqual(runner.verbs, [])

        sl.plist_path(self.os_home, agent=sl.LEGACY_DRAIN_AGENT).write_bytes(b"not a plist")
        unreadable = sl.remove_legacy_drain(os_home=self.os_home, runner=_LegacyBootoutSucceeds())
        self.assertEqual(unreadable["reason"], sl.REASON_LEGACY_DRAIN_UNREADABLE)

    def test_the_refusal_carries_no_manager_text_or_secret(self) -> None:
        _write_home_credential(self.mozyo_home)
        _legacy_drain_plist(self.os_home)
        runner = _BootoutFails(
            _result(113, stderr='Could not find service "x" — home-key-sentinel /Users/someone')
        )
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found,
        )
        blob = str(result)
        self.assertNotIn("home-key", blob.lower())
        self.assertNotIn("/Users/someone", blob)
        self.assertNotIn("Could not find service", blob)


class _BootoutFails:
    """launchctl where the RETIRED label's bootout fails; `print` is scripted but must go unused."""

    def __init__(self, print_result) -> None:
        self.calls: list[list[str]] = []
        self._print_result = print_result

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        target = argv[2] if len(argv) > 2 else ""
        if sl.LEGACY_DRAIN_AGENT.label in target:
            if argv[1] == "bootout":
                return _result(1, stderr="bootout failed")
            if argv[1] == "print":
                return self._print_result
        return _result(0)

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) >= 2]


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


class OwnedIdentityIsRevalidatedAtActionTimeTest(_DarwinCase):
    """Every destructive verb re-establishes ownership at the moment it mutates (j#102496 r12f1).

    The entry classification and the mutation are separated by a subprocess call, so they are two
    different facts about two different moments. These pin the second one. They do not claim the
    race is closed — ``unlink`` / ``write_bytes`` target a path, not the validated inode — only that
    a replacement this adapter *can* observe is never mutated.
    """

    def _swap_on(self, verb: str, target: Path, label: str, *, remove: bool = False):
        """A runner that replaces ``target`` while ``verb`` runs, i.e. inside the stale window."""
        def runner(argv):
            if len(argv) >= 2 and argv[1] == verb:
                if remove:
                    target.unlink()
                else:
                    _write_plist(target, label)
            return _result(0)
        return runner

    def test_legacy_plist_replaced_during_bootout_is_not_unlinked(self) -> None:
        legacy = _legacy_drain_plist(self.os_home)
        result = sl.remove_legacy_drain(
            os_home=self.os_home,
            runner=self._swap_on("bootout", legacy, "com.example.foreign"),
        )
        self.assertFalse(result["removed"])
        self.assertEqual(sl.REASON_LEGACY_DRAIN_FOREIGN_LABEL, result["reason"])
        self.assertEqual(sl.LEGACY_DRAIN_FOREIGN, result["state"])
        self.assertTrue(legacy.exists())  # the stranger's file survived
        self.assertEqual("com.example.foreign", plistlib.loads(legacy.read_bytes())["Label"])

    def test_legacy_plist_made_unreadable_during_bootout_is_not_unlinked(self) -> None:
        legacy = _legacy_drain_plist(self.os_home)

        def runner(argv):
            if len(argv) >= 2 and argv[1] == "bootout":
                legacy.write_bytes(b"\x00 truncated mid-write")
            return _result(0)

        result = sl.remove_legacy_drain(os_home=self.os_home, runner=runner)
        self.assertFalse(result["removed"])
        self.assertEqual(sl.REASON_LEGACY_DRAIN_UNREADABLE, result["reason"])
        self.assertTrue(legacy.exists())

    def test_legacy_plist_removed_by_someone_else_is_not_claimed_as_our_removal(self) -> None:
        # The goal state holds, but this call did not bring it about. Reporting `removed: True`
        # would credit us with a mutation we never performed.
        legacy = _legacy_drain_plist(self.os_home)
        result = sl.remove_legacy_drain(
            os_home=self.os_home, runner=self._swap_on("bootout", legacy, "", remove=True)
        )
        self.assertFalse(result["removed"])
        self.assertEqual("", result["reason"])
        self.assertEqual(sl.LEGACY_DRAIN_ABSENT, result["state"])

    def test_owned_plist_replaced_during_uninstall_bootout_is_not_unlinked(self) -> None:
        owned = _write_plist(sl.plist_path(self.os_home), sl.SUPERVISOR_LAUNCHD_LABEL)
        result = sl.uninstall(
            os_home=self.os_home,
            runner=self._swap_on("bootout", owned, "com.example.foreign"),
        )
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual(sl.PLIST_FOREIGN, result["plist_state"])
        self.assertTrue(owned.exists())

    def test_current_plist_replaced_during_migration_is_not_overwritten_by_install(self) -> None:
        # install's own-path preflight runs before the migration; the migration then shells out, so
        # the write is re-checked against what is on disk *now*.
        _legacy_drain_plist(self.os_home)
        current = sl.plist_path(self.os_home)
        result = sl.install(
            os_home=self.os_home,
            mozyo_home=self.mozyo_home,
            runner=self._swap_on("bootout", current, "com.example.foreign"),
            which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual("com.example.foreign", plistlib.loads(current.read_bytes())["Label"])


class CurrentAgentIdentityFenceTest(_DarwinCase):
    """install / uninstall mutate our own path only when the file there is ours (j#102496 r12f2).

    The retired-drain path had this fence from the start; the current agent's did not, so a plist
    carrying a stranger's ``Label`` was overwritten by install and deleted by uninstall. A path is a
    location — the ``Label`` inside the file is the identity.
    """

    def _foreign(self) -> Path:
        return _write_plist(sl.plist_path(self.os_home), "com.example.foreign")

    def _unreadable(self) -> Path:
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"this is not a plist")
        return target

    def test_install_refuses_over_a_foreign_plist_with_zero_mutation(self) -> None:
        foreign = self._foreign()
        before = foreign.read_bytes()
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual(sl.PLIST_FOREIGN, result["plist_state"])
        self.assertEqual(before, foreign.read_bytes())
        self.assertEqual([], runner.calls)  # not even a bootout

    def test_install_refuses_over_an_unreadable_plist_with_zero_mutation(self) -> None:
        unreadable = self._unreadable()
        runner = FakeRunner()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
        self.assertEqual(b"this is not a plist", unreadable.read_bytes())
        self.assertEqual([], runner.calls)

    def test_install_refusal_does_not_remove_the_retired_drain_first(self) -> None:
        # The own-path preflight is evaluated BEFORE the migration, so a refused install really is
        # zero-mutation rather than "zero mutation apart from the one it already did".
        legacy = _legacy_drain_plist(self.os_home)
        self._foreign()
        result = sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(), which=_which_found,
        )
        self.assertFalse(result["performed"])
        self.assertTrue(legacy.exists())

    def test_uninstall_refuses_to_delete_a_foreign_plist(self) -> None:
        foreign = self._foreign()
        runner = FakeRunner()
        result = sl.uninstall(os_home=self.os_home, runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual(sl.PLIST_FOREIGN, result["plist_state"])
        self.assertTrue(foreign.exists())
        self.assertEqual([], runner.calls)

    def test_uninstall_refuses_to_delete_an_unreadable_plist(self) -> None:
        unreadable = self._unreadable()
        runner = FakeRunner()
        result = sl.uninstall(os_home=self.os_home, runner=runner)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
        self.assertTrue(unreadable.exists())
        self.assertEqual([], runner.calls)

    def test_uninstall_over_a_foreign_plist_leaves_the_retired_drain_alone_too(self) -> None:
        # An unidentifiable host state stops the whole verb, not just the part that touches it.
        legacy = _legacy_drain_plist(self.os_home)
        self._foreign()
        sl.uninstall(os_home=self.os_home, runner=FakeRunner())
        self.assertTrue(legacy.exists())

    def test_refusals_name_no_path_no_label_and_no_manager_text(self) -> None:
        self._foreign()
        rendered = repr(sl.uninstall(os_home=self.os_home, runner=FakeRunner())) + repr(
            sl.install(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=FakeRunner(), which=_which_found,
            )
        )
        self.assertNotIn(str(self.os_home), rendered)
        self.assertNotIn("com.example.foreign", rendered)
        self.assertNotIn("Boot-out", rendered)


class UninstallNeverDeletesAfterFailedBootoutTest(_DarwinCase):
    """A succeeding bootout is the only authority to unlink the owned plist (j#102496 r12f3).

    The same rule the retired-drain migration already carries (j#102458), applied to the verb that
    deletes the *current* agent. Unlinking does not unregister anything — launchd keys a bootstrapped
    job off its label — so deleting after a failed bootout hides a possibly-live job. It used to do
    exactly that while reporting ``performed: true`` / ``removed: true`` and an empty reason.
    """

    def setUp(self) -> None:
        super().setUp()
        self.owned = _write_plist(sl.plist_path(self.os_home), sl.SUPERVISOR_LAUNCHD_LABEL)

    def _uninstall_with(self, result_or_exc):
        calls: list[list[str]] = []

        def runner(argv):
            calls.append(list(argv))
            if isinstance(result_or_exc, Exception):
                raise result_or_exc
            return result_or_exc

        return sl.uninstall(os_home=self.os_home, runner=runner), calls

    def test_nonzero_bootout_keeps_the_plist_and_refuses(self) -> None:
        for stderr in (
            "Boot-out failed: 5: Input/output error",
            'Could not find service "org.mozyo-bridge.callback-supervisor" in domain',
            "",
        ):
            with self.subTest(stderr=stderr):
                _write_plist(sl.plist_path(self.os_home), sl.SUPERVISOR_LAUNCHD_LABEL)
                result, _ = self._uninstall_with(_result(1, stderr=stderr))
                self.assertFalse(result["performed"])
                self.assertEqual(sl.REASON_BOOTOUT_FAILED, result["reason"])
                self.assertTrue(self.owned.exists())

    def test_wording_that_claims_absence_still_does_not_authorize_the_unlink(self) -> None:
        # The retired second authority: "bootout failed but the manager says it is unknown, so it
        # was never loaded". No deletion reads manager wording any more (j#102458 / r12f4).
        result, calls = self._uninstall_with(
            _result(113, stderr='Could not find service "org.mozyo-bridge.callback-supervisor"')
        )
        self.assertFalse(result["performed"])
        self.assertTrue(self.owned.exists())
        self.assertEqual(["bootout"], [c[1] for c in calls])  # no `print` follow-up at all

    def test_a_bootout_that_cannot_run_keeps_the_plist(self) -> None:
        for exc in (FileNotFoundError("launchctl"), OSError("denied")):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(type(exc)):
                    self._uninstall_with(exc)
                self.assertTrue(self.owned.exists())

    def test_a_succeeding_bootout_still_removes_the_owned_plist(self) -> None:
        result, calls = self._uninstall_with(_result(0))
        self.assertTrue(result["performed"])
        self.assertTrue(result["removed"])
        self.assertEqual(sl.PLIST_OWNED, result["plist_state"])
        self.assertFalse(self.owned.exists())
        # One bootout for the owned label; the retired drain is absent here, and its migration
        # short-circuits on `absent` without shelling out at all.
        self.assertEqual(["bootout"], [c[1] for c in calls])

    def test_a_succeeding_bootout_removes_the_retired_drain_in_the_same_run(self) -> None:
        legacy = _legacy_drain_plist(self.os_home)
        result, calls = self._uninstall_with(_result(0))
        self.assertTrue(result["removed"])
        self.assertTrue(result["legacy_drain_removed"])
        self.assertFalse(legacy.exists())
        self.assertEqual(["bootout", "bootout"], [c[1] for c in calls])  # owned, then retired drain

    def test_an_absent_plist_boots_out_and_succeeds_only_when_the_bootout_does(self) -> None:
        # A loaded job whose file is already gone is exactly the state a teardown should clear — so
        # whether the bootout SUCCEEDED is precisely what decides the answer. This test previously
        # asserted the opposite (rc=1 -> performed True), pinning a success the host had not
        # delivered; the comment even said the exit code "decides nothing" while the branch above it
        # explained why it decides everything (review j#102550 r13f3).
        self.owned.unlink()
        result, calls = self._uninstall_with(_result(0))
        self.assertTrue(result["performed"])
        self.assertFalse(result["removed"])
        self.assertEqual(sl.PLIST_ABSENT, result["plist_state"])
        self.assertIn("bootout", [c[1] for c in calls])

    def test_an_absent_plist_with_a_failed_bootout_refuses(self) -> None:
        # Nothing to keep and nothing removable, but the job may still be running, so this is a
        # refusal — not a quiet success with CLI exit 0.
        self.owned.unlink()
        result, _ = self._uninstall_with(_result(1, stderr="Boot-out failed: 5: I/O error"))
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_BOOTOUT_FAILED, result["reason"])
        self.assertEqual(sl.PLIST_ABSENT, result["plist_state"])

    def test_an_unlink_failure_is_a_typed_result_not_an_escaping_oserror(self) -> None:
        # The retired-drain migration has always reported this as a typed result; this verb let the
        # OSError out of the envelope, host path and all (review j#102550 r13f5).
        with patch.object(os, "unlink", side_effect=OSError("read-only file system")):
            result, _ = self._uninstall_with(_result(0))
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_REMOVAL_FAILED, result["reason"])
        self.assertFalse(result["removed"])
        self.assertEqual(sl.PLIST_OWNED, result["plist_state"])
        rendered = repr(result)
        self.assertNotIn("read-only", rendered)
        self.assertNotIn(str(self.os_home), rendered)

    def test_the_refusal_carries_no_manager_text_or_host_path(self) -> None:
        result, _ = self._uninstall_with(
            _result(1, stderr=f"Boot-out failed at {self.os_home}: 5: Input/output error")
        )
        rendered = repr(result)
        self.assertNotIn(str(self.os_home), rendered)
        self.assertNotIn("Boot-out", rendered)
        self.assertNotIn("Input/output", rendered)


class RestartEnforcesOwnedIdentityTest(_DarwinCase):
    """restart acts only on a plist that is ours (review j#102550 r13f1).

    It was the one verb reading the plist's *contents* — argv, `--home` pin — without ever asking
    whose plist it was. A stranger's file carrying our expected ProgramArguments produced a
    `performed: true` kickstart, and the kickstart named OUR label: the evidence and the action were
    about different services.
    """

    def _expected_argv(self) -> list:
        return sl.resolve_supervisor_command(mozyo_home=self.mozyo_home, which=_which_found)

    def _plist_with(self, label: str) -> Path:
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": self._expected_argv()})
        )
        return target

    def test_a_foreign_plist_with_our_exact_argv_is_refused_before_any_launchctl(self) -> None:
        self._plist_with("com.example.foreign")
        runner = FakeRunner(print_result=_result(0, "\tstate = running\n\tpid = 42\n"))
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual(sl.PLIST_FOREIGN, result["plist_state"])
        self.assertEqual([], runner.calls)  # no print, and above all no kickstart

    def test_an_unreadable_plist_refuses_and_still_reports_the_pin_field(self) -> None:
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not a plist")
        runner = FakeRunner()
        result = sl.restart(os_home=self.os_home, runner=runner, which=_which_found)
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
        # `home_pin` keeps the value it always had, so consumers of that field are unaffected; only
        # the reason changed, to the accurate one.
        self.assertEqual(sl.HOME_PIN_UNREADABLE, result["home_pin"])
        self.assertEqual([], runner.calls)

    def test_a_plist_swapped_between_the_probe_and_the_kickstart_is_not_kickstarted(self) -> None:
        # Same window r12f1 closed for the destructive verbs: a kickstart is a mutation too.
        target = self._plist_with(sl.SUPERVISOR_LAUNCHD_LABEL)
        calls: list[str] = []

        def runner(argv):
            calls.append(argv[1])
            if argv[1] == "print":
                target.write_bytes(
                    plistlib.dumps(
                        {"Label": "com.example.foreign", "ProgramArguments": self._expected_argv()}
                    )
                )
                return _result(0, "\tstate = running\n\tpid = 42\n")
            return _result(0)

        result = sl.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
        self.assertEqual(["print"], calls)  # the kickstart never went out

    def test_an_owned_plist_still_restarts(self) -> None:
        self._plist_with(sl.SUPERVISOR_LAUNCHD_LABEL)
        runner = FakeRunner(print_result=_result(0, "\tstate = running\n\tpid = 42\n"))
        result = sl.restart(
            os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner, which=_which_found
        )
        self.assertTrue(result["performed"], result.get("reason"))
        self.assertEqual(["print", "kickstart"], runner.verbs)


class ThePathIsNotFollowedTest(_DarwinCase):
    """A symlink at the owned path never redirects a mutation out of it (j#102550 r13f2).

    r12f2 established that a path does not prove ownership of the file at it. This is the mirror
    image: the path may not even *be* the file. `Path.exists()` calls a broken symlink absent, so a
    link classified as `absent` and the install created a file wherever it pointed; a link to an
    existing plist carrying our label classified as `owned` and the install overwrote it. Both
    reported `performed: true`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        self.target = sl.plist_path(self.os_home)
        self.target.parent.mkdir(parents=True, exist_ok=True)

    def _install(self):
        return sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(), which=_which_found,
        )

    def test_a_broken_symlink_is_unreadable_not_absent(self) -> None:
        self.target.symlink_to(self.outside / "nowhere.plist")
        self.assertEqual(sl.PLIST_UNREADABLE, sl.classify_agent_plist(self.os_home))

    def test_install_through_a_broken_symlink_creates_nothing_outside(self) -> None:
        victim = self.outside / "victim.plist"
        self.target.symlink_to(victim)
        result = self._install()
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
        self.assertFalse(victim.exists())

    def test_install_through_a_symlink_to_our_label_does_not_overwrite_it(self) -> None:
        victim = _write_plist(self.outside / "someone-elses.plist", sl.SUPERVISOR_LAUNCHD_LABEL)
        before = victim.read_bytes()
        self.target.symlink_to(victim)
        result = self._install()
        self.assertFalse(result["performed"])
        self.assertEqual(before, victim.read_bytes())

    def test_uninstall_and_restart_refuse_a_symlink_too(self) -> None:
        victim = _write_plist(self.outside / "elsewhere.plist", sl.SUPERVISOR_LAUNCHD_LABEL)
        self.target.symlink_to(victim)
        for verb, call in (
            ("uninstall", lambda: sl.uninstall(os_home=self.os_home, runner=FakeRunner())),
            ("restart", lambda: sl.restart(
                os_home=self.os_home, runner=FakeRunner(), which=_which_found)),
        ):
            with self.subTest(verb=verb):
                result = call()
                self.assertFalse(result["performed"])
                self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
                self.assertTrue(victim.exists())

    def test_a_hard_linked_plist_is_refused_and_the_other_name_is_untouched(self) -> None:
        # Same class: the inode is reachable under a name we never accounted for, so writing "our"
        # path writes a file outside it. A deliberate, stated refusal.
        other = self.outside / "second-name.plist"
        _write_plist(self.target, sl.SUPERVISOR_LAUNCHD_LABEL)
        os.link(self.target, other)
        before = other.read_bytes()
        self.assertEqual(sl.PLIST_UNREADABLE, sl.classify_agent_plist(self.os_home))
        result = self._install()
        self.assertFalse(result["performed"])
        self.assertEqual(before, other.read_bytes())

    def test_the_writer_itself_refuses_a_symlink_even_when_asked_directly(self) -> None:
        # The guarantee must not rest on the classification winning a race, so the write refuses to
        # follow a link on its own rather than trusting the check that preceded it. Since j#102590
        # r14f2 it stages and renames, so the link is replaced as a NAME and the file it pointed at
        # keeps its contents — which is the property that matters.
        victim = self.outside / "late-swap.plist"
        victim.write_bytes(b"someone else's bytes")
        self.target.symlink_to(victim)
        fs.write_owned(b"payload", self.os_home)
        self.assertEqual(b"someone else's bytes", victim.read_bytes())
        self.assertFalse(self.target.is_symlink())

    def test_a_plain_owned_plist_is_still_written_and_readable(self) -> None:
        result = self._install()
        self.assertTrue(result["performed"], result.get("reason"))
        self.assertEqual(
            sl.SUPERVISOR_LAUNCHD_LABEL, plistlib.loads(self.target.read_bytes())["Label"]
        )


class StatusReadsOnlyOurOwnPlistTest(_DarwinCase):
    """service_status never reads out a file it did not write (review j#102550 r13f4).

    The secret-free promise held for a plist this adapter rendered — no environment block, no
    credential. It was being applied to whatever occupied the path, so a foreign plist's
    `ProgramArguments` reached the payload verbatim.
    """

    SENTINEL = "sensitive-sentinel-value"

    def _status(self):
        return sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=_result(113)), which=_which_found,
        )

    def _foreign_with_sentinel(self) -> Path:
        target = sl.plist_path(self.os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.example.foreign",
                    "ProgramArguments": ["/tmp/foreign", "--token", self.SENTINEL],
                }
            )
        )
        return target

    def test_a_foreign_plists_arguments_never_reach_the_projection(self) -> None:
        self._foreign_with_sentinel()
        status = self._status()
        self.assertNotIn(self.SENTINEL, repr(status))
        self.assertEqual([], status["installed_command"])
        self.assertEqual(sl.PLIST_FOREIGN, status["plist_state"])

    def test_status_still_reports_that_something_is_installed(self) -> None:
        # Suppressing the contents must not suppress the fact: an operator has to see that a file is
        # there and that it is not ours.
        self._foreign_with_sentinel()
        status = self._status()
        self.assertTrue(status["plist_exists"])
        self.assertEqual(sl.PLIST_FOREIGN, status["plist_state"])
        self.assertEqual(sl.HOME_PIN_UNREADABLE, status["home_pin"])
        self.assertFalse(status["executable_matches"])

    def test_an_owned_plist_still_publishes_its_argv(self) -> None:
        sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(), which=_which_found,
        )
        status = self._status()
        self.assertEqual(sl.PLIST_OWNED, status["plist_state"])
        self.assertIn("--run-once", status["installed_command"])

    def test_a_clean_host_reports_absent(self) -> None:
        status = self._status()
        self.assertEqual(sl.PLIST_ABSENT, status["plist_state"])
        self.assertFalse(status["plist_exists"])
        self.assertEqual([], status["installed_command"])


class NoAncestorEscapesTheOwnedPathTest(_DarwinCase):
    """Every component of the owned path is checked, not just the last (j#102590 r14f1).

    `lstat` and `O_NOFOLLOW` apply to the FINAL component only, so making `Library/LaunchAgents` a
    symlink left every leaf check intact and still put the write, the read and the unlink in someone
    else's directory — with `performed: true` and, for a plist planted there, a foreign argv
    published as `owned`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp())
        (self.os_home / "Library").mkdir(parents=True, exist_ok=True)
        (self.os_home / "Library" / "LaunchAgents").symlink_to(self.outside)

    def _install(self):
        return sl.install(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(), which=_which_found,
        )

    def test_a_symlinked_ancestor_is_unreadable_not_absent(self) -> None:
        self.assertEqual(sl.PLIST_UNREADABLE, sl.classify_agent_plist(self.os_home))

    def test_install_creates_nothing_beyond_a_symlinked_ancestor(self) -> None:
        result = self._install()
        self.assertFalse(result["performed"])
        # Both plists live under the relinked directory, so whichever preflight runs first reports
        # it. The retired drain's comes first, and either token is an accurate account of the same
        # fact: nothing under this path can be identified.
        self.assertIn(
            result["reason"], (sl.REASON_LEGACY_DRAIN_UNREADABLE, sl.REASON_PLIST_UNREADABLE)
        )
        self.assertEqual([], list(self.outside.iterdir()))

    def test_status_publishes_no_argv_from_beyond_a_symlinked_ancestor(self) -> None:
        _write_plist(self.outside / f"{sl.SUPERVISOR_LAUNCHD_LABEL}.plist", sl.SUPERVISOR_LAUNCHD_LABEL)
        (self.outside / f"{sl.SUPERVISOR_LAUNCHD_LABEL}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": sl.SUPERVISOR_LAUNCHD_LABEL,
                    "ProgramArguments": ["/x", "--token", "ANCESTOR-SENTINEL"],
                }
            )
        )
        status = sl.service_status(
            os_home=self.os_home, mozyo_home=self.mozyo_home,
            runner=FakeRunner(print_result=_result(113)), which=_which_found,
        )
        self.assertEqual(sl.PLIST_UNREADABLE, status["plist_state"])
        self.assertNotIn("ANCESTOR-SENTINEL", repr(status))

    def test_uninstall_and_restart_refuse_beyond_a_symlinked_ancestor(self) -> None:
        planted = self.outside / f"{sl.SUPERVISOR_LAUNCHD_LABEL}.plist"
        _write_plist(planted, sl.SUPERVISOR_LAUNCHD_LABEL)
        for verb, call in (
            ("uninstall", lambda: sl.uninstall(os_home=self.os_home, runner=FakeRunner())),
            ("restart", lambda: sl.restart(
                os_home=self.os_home, runner=FakeRunner(), which=_which_found)),
        ):
            with self.subTest(verb=verb):
                result = call()
                self.assertFalse(result["performed"])
                self.assertEqual(sl.REASON_PLIST_UNREADABLE, result["reason"])
                self.assertTrue(planted.exists())

    def test_the_retired_migration_refuses_beyond_a_symlinked_ancestor_too(self) -> None:
        planted = self.outside / f"{sl.SUPERVISOR_DRAIN_LAUNCHD_LABEL}.plist"
        _write_plist(planted, sl.SUPERVISOR_DRAIN_LAUNCHD_LABEL)
        result = sl.remove_legacy_drain(os_home=self.os_home, runner=FakeRunner())
        self.assertFalse(result["removed"])
        self.assertEqual(sl.LEGACY_DRAIN_UNREADABLE, result["state"])
        self.assertTrue(planted.exists())

    def test_a_missing_directory_is_absence_not_an_unreadable_state(self) -> None:
        # A directory that cannot exist cannot hold a plist. Conflating the two would make every
        # never-installed host look unidentifiable and refuse its first install.
        clean = Path(tempfile.mkdtemp())
        self.assertEqual(sl.PLIST_ABSENT, sl.classify_agent_plist(clean))


class TheWriteIsStagedNotTruncatedTest(_DarwinCase):
    """A write assembles under a temporary name and is renamed into place (j#102590 r14f2).

    Opening with `O_TRUNC` destroyed the destination before the descriptor could be examined, so a
    plist swapped for a hard link to someone else's file had that file overwritten — through a fence
    that had just refused hard links. And a partial write left a truncated plist reporting success.
    """

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(tempfile.mkdtemp())
        self.target = sl.plist_path(self.os_home)
        self.target.parent.mkdir(parents=True, exist_ok=True)

    def test_a_hard_link_swapped_in_before_the_write_keeps_its_contents(self) -> None:
        victim = _write_plist(self.outside / "victim.plist", "com.example.victim")
        original = victim.read_bytes()
        os.link(victim, self.target)
        fs.write_owned(b"ours", self.os_home)
        # The rename replaced a NAME. The other name still refers to the untouched inode.
        self.assertEqual(original, victim.read_bytes())
        self.assertEqual(b"ours", self.target.read_bytes())
        self.assertEqual(1, os.stat(victim).st_nlink)

    def test_a_failure_mid_write_leaves_the_previous_plist_intact(self) -> None:
        _write_plist(self.target, sl.SUPERVISOR_LAUNCHD_LABEL)
        before = self.target.read_bytes()
        real_write, calls = os.write, {"n": 0}

        def failing(fd, data):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write(fd, data[:1])

        with patch.object(os, "write", failing):
            with self.assertRaises(OSError):
                fs.write_owned(b"0123456789" * 40, self.os_home)
        self.assertEqual(before, self.target.read_bytes())

    def test_a_failed_write_leaves_no_staging_file_behind(self) -> None:
        _write_plist(self.target, sl.SUPERVISOR_LAUNCHD_LABEL)
        with patch.object(os, "write", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                fs.write_owned(b"payload", self.os_home)
        self.assertEqual([self.target.name], [p.name for p in self.target.parent.iterdir()])

    def test_a_partial_write_is_completed_rather_than_reported_as_done(self) -> None:
        # `os.write` may write fewer bytes than it is given; the loop must finish the payload.
        payload = b"0123456789" * 40
        real_write = os.write
        with patch.object(os, "write", lambda fd, data: real_write(fd, data[:7])):
            fs.write_owned(payload, self.os_home)
        self.assertEqual(payload, self.target.read_bytes())

    def test_two_concurrent_writers_never_rename_each_others_payload(self) -> None:
        payloads = (b"writer-a:" + b"A" * 4096, b"writer-b:" + b"B" * 4096)
        both_staged = threading.Barrier(2)
        real_write_all = fs._write_all
        errors: list[BaseException] = []

        def staged_write(fd, payload):
            real_write_all(fd, payload)
            both_staged.wait(timeout=5)

        def writer(payload):
            try:
                fs.write_owned(payload, self.os_home)
            except BaseException as exc:  # captured in the test thread and asserted below
                errors.append(exc)

        with patch.object(fs, "_write_all", staged_write):
            threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertIn(self.target.read_bytes(), payloads)
        self.assertEqual([self.target.name], sorted(p.name for p in self.target.parent.iterdir()))

    def test_unknown_historical_and_foreign_staging_entries_are_never_deleted(self) -> None:
        historical = self.target.with_name(f"{self.target.name}.mozyo-staging")
        foreign = self.target.with_name(f".{self.target.name}.mozyo-staging-foreign")
        historical.write_bytes(b"historical-writer")
        foreign.write_bytes(b"concurrent-writer")

        fs.write_owned(b"ours", self.os_home)

        self.assertEqual(b"ours", self.target.read_bytes())
        self.assertEqual(b"historical-writer", historical.read_bytes())
        self.assertEqual(b"concurrent-writer", foreign.read_bytes())


class StatusJudgesAndPublishesOneInodeTest(_DarwinCase):
    """The verdict and the bytes come from a single descriptor (review j#102590 r14f3).

    Classifying by path and then re-opening it to read judged one file and published another: a
    plist replaced between the two calls was reported `owned` while a stranger's argv went out.
    """

    def test_a_swap_after_classification_cannot_publish_its_argv(self) -> None:
        target = sl.plist_path(self.os_home)
        _write_plist(target, sl.SUPERVISOR_LAUNCHD_LABEL)
        real_read = fs._read_all

        def swap_then_read(fd):
            # Replace the path — not the descriptor — with an owned-looking file carrying a secret.
            target.unlink()
            target.write_bytes(
                plistlib.dumps(
                    {
                        "Label": sl.SUPERVISOR_LAUNCHD_LABEL,
                        "ProgramArguments": ["/x", "--token", "SWAP-SENTINEL"],
                    }
                )
            )
            return real_read(fd)

        with patch.object(fs, "_read_all", swap_then_read):
            status = sl.service_status(
                os_home=self.os_home, mozyo_home=self.mozyo_home,
                runner=FakeRunner(print_result=_result(113)), which=_which_found,
            )
        self.assertNotIn("SWAP-SENTINEL", repr(status))

    def test_read_owned_returns_no_bytes_for_anything_it_did_not_authenticate(self) -> None:
        target = sl.plist_path(self.os_home)
        for label, expected in (
            ("com.example.foreign", sl.PLIST_FOREIGN),
            (sl.SUPERVISOR_LAUNCHD_LABEL, sl.PLIST_OWNED),
        ):
            with self.subTest(label=label):
                _write_plist(target, label)
                state, payload = fs.read_owned(self.os_home)
                self.assertEqual(expected, state)
                self.assertEqual(expected == sl.PLIST_OWNED, bool(payload))


if __name__ == "__main__":
    unittest.main()
