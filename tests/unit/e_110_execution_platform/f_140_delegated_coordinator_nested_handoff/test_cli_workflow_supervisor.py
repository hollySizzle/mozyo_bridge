"""`workflow supervisor` CLI tests (Redmine #13683 Phase B1; #15183 backend dispatch).

Pins the facade: run-once / status over a hermetic temp home, and the service lifecycle command
contract — service-status is a redacted projection + secret-free definition (exit 0), while
install / restart / uninstall drive the owned scheduler pair and fail-closed (exit non-zero, zero
mutation) when the host cannot run it.

Redmine #15183: the same four verbs now dispatch by platform — the macOS LaunchAgent pair on darwin,
the systemd **user** service+timer pair on Linux, a typed refusal anywhere else — so every service
test below pins the backend it means to exercise instead of inheriting the runner's OS. Real
``launchctl`` / ``systemctl`` are never invoked here (patched), and both the OS user home and
``XDG_CONFIG_HOME`` are isolated so a projection never reads or writes the host's real
``~/Library/LaunchAgents`` or ``~/.config/systemd/user`` (Redmine #14103).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_launchd as sl,
    supervisor_service_backend as sb,
    supervisor_systemd as ss,
)


def _fake_run(argv, *a, **k):
    return type("R", (), {"returncode": 113, "stdout": "", "stderr": "not found"})()


def _run(argv) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


class _ServiceCliCase(unittest.TestCase):
    """Base: a hermetic mozyo home + OS user home, with the host scheduler roots isolated."""

    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))
        # A hermetic OS user home for the owned scheduler artifacts. --home is the mozyo home and
        # by design never relocates them, so their root is isolated via Path.home() — and, for the
        # systemd adapter, by pointing XDG_CONFIG_HOME at the same temp root (its unit directory
        # honours that variable exactly as the user manager does).
        self.os_home = Path(tempfile.mkdtemp())

    @contextlib.contextmanager
    def _isolated_host(self, platform: str, *, run=_fake_run):
        """Pin the dispatched backend AND isolate every host root it would touch."""
        module = sl if platform == "darwin" else ss
        with patch.object(sys, "platform", platform), patch.object(
            module.subprocess, "run", side_effect=run
        ), patch("pathlib.Path.home", return_value=self.os_home), patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.os_home / ".config")}, clear=False
        ):
            yield

    def _service_status(self, platform: str, *, run=_fake_run) -> tuple[int, str]:
        with self._isolated_host(platform, run=run):
            return _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
            )


class CliServiceStatusLaunchdTest(_ServiceCliCase):
    """The darwin dispatch: the owned LaunchAgent pair answers ``--service-status``."""

    def test_service_status_reports_projection_and_definition_exit_zero(self) -> None:
        rc, out = self._service_status("darwin")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_LAUNCHD)
        # Redmine #14150: the projection is the owned PAIR (reconcile + drain agents).
        agents = payload["agents"]
        self.assertEqual(len(agents), 2)
        reconcile, drain = agents
        self.assertFalse(reconcile["installed"])
        self.assertFalse(reconcile["loaded"])
        self.assertFalse(drain["installed"])
        self.assertEqual(payload["phase"], "B1")
        self.assertFalse(reconcile["keep_alive_present"])
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        self.assertEqual(payload["drain_definition"]["command"][-1], "--drain-only")
        self.assertFalse(payload["definition"]["keep_alive"])
        # macOS keeps its own two-row shape; #15183 does not reorganize it.
        self.assertEqual(len(agents), 2)
        # The two agents are distinct owned labels.
        self.assertNotEqual(reconcile["label"], drain["label"])
        # Secret-free and path-free.
        self.assertNotIn("api_key", out.lower())
        self.assertNotIn(self.home, out)

    def test_service_status_reports_installed_when_owned_plist_present(self) -> None:
        # Positive verdict held deterministic by the same OS-home seam: an owned
        # plist under the isolated home is reported installed, proving the
        # projection reflects the controlled home rather than being always-false.
        target = sl.plist_path(self.os_home)  # default agent = reconcile
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", self.home]
        target.write_bytes(sl.render_plist(argv, interval_seconds=300, os_home=self.os_home))
        rc, out = self._service_status("darwin")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        reconcile = payload["agents"][0]
        self.assertTrue(reconcile["installed"])  # the reconcile agent's owned plist is present
        self.assertTrue(reconcile["plist_exists"])
        # The drain agent was NOT installed, so the pair projection distinguishes them.
        self.assertFalse(payload["agents"][1]["installed"])

    def test_mutating_verbs_fail_closed_zero_mutation_when_launchd_refuses(self) -> None:
        with patch.object(sl, "_running_on_darwin", return_value=False), patch.object(
            sys, "platform", "darwin"
        ), patch.object(sl.subprocess, "run", side_effect=AssertionError("launchctl must not run")):
            for verb in ("--install", "--restart", "--uninstall"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
                payload = json.loads(out)
                self.assertEqual(rc, 1, verb)
                self.assertFalse(payload["performed"], verb)
                self.assertEqual(payload["reason"], sl.REASON_UNSUPPORTED_PLATFORM, verb)


class CliServiceStatusSystemdTest(_ServiceCliCase):
    """The Linux dispatch (Redmine #15183): ONE owned systemd user service + timer, same verbs."""

    def test_service_status_reports_the_systemd_projection_exit_zero(self) -> None:
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_SYSTEMD)
        # ONE owned service on Linux -- not the macOS two-row roster.
        self.assertEqual(len(payload["agents"]), 1)
        row = payload["agents"][0]
        self.assertFalse(row["installed"])
        self.assertFalse(row["loaded"])
        self.assertEqual(row["service_unit"], ss.SERVICE_UNIT_NAME)
        self.assertEqual(row["timer_unit"], ss.TIMER_UNIT_NAME)
        # The declarative definition stays the secret-free bounded command on both backends.
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        self.assertNotIn("api_key", out.lower())
        self.assertNotIn(self.home, out)

    def test_service_status_shows_next_run_and_last_exit_result(self) -> None:
        # The acceptance contract asks status to show 次回起動 / 直近の終了結果 without secrets.
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        row = json.loads(out)["agents"][0]
        for key in ("next_elapse", "next_elapse_basis", "last_trigger",
                    "last_result", "last_exit_status", "last_exit_at"):
            self.assertIn(key, row)

    def test_service_status_reports_installed_when_the_owned_units_are_present(self) -> None:
        unit = ss.SUPERVISOR_UNIT
        path = ss.service_unit_path(self.os_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ss.render_service_unit(
                ["/opt/bin/mozyo-bridge", *unit.argv_tail, "--home", self.home]
            ),
            encoding="utf-8",
        )
        ss.timer_unit_path(self.os_home).write_text(
            ss.render_timer_unit(interval_seconds=unit.default_interval_seconds), encoding="utf-8"
        )
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["agents"][0]["installed"])
        self.assertEqual(payload["agents"][0]["scheduled_interval_seconds"], 60)
        self.assertEqual(payload["agents"][0]["installed_command"][-1], self.home)

    def test_mutating_verbs_fail_closed_zero_mutation_with_no_user_manager(self) -> None:
        # A container with no user bus is explicitly unsupported, never a silent degrade to
        # "installed but never scheduled".
        def _no_manager(argv, *a, **k):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no bus"})()

        for verb in ("--install", "--restart", "--uninstall"):
            with self._isolated_host("linux", run=_no_manager):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["reason"], ss.REASON_USER_MANAGER_UNAVAILABLE, verb)
            self.assertEqual(payload["backend"], sb.BACKEND_SYSTEMD, verb)
        self.assertFalse(ss.unit_dir(self.os_home).exists())


class CliServiceUnsupportedHostTest(_ServiceCliCase):
    """A host with no owned scheduler adapter is a typed refusal, never a silent no-op."""

    def test_mutating_verbs_refuse_with_a_typed_backend_token(self) -> None:
        for verb in ("--install", "--restart", "--uninstall"):
            with patch.object(sys, "platform", "win32"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["reason"], sb.REASON_NO_BACKEND, verb)
            self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED, verb)

    def test_service_status_still_exits_zero_and_mutates_nothing(self) -> None:
        with patch.object(sys, "platform", "win32"):
            rc, out = _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED)
        self.assertEqual(payload["agents"], [])
        self.assertFalse(payload["platform_supported"])
        # The secret-free declarative definitions are still projected.
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")


class CliWorkflowSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))

    def test_status_over_empty_home_exits_zero(self) -> None:
        rc, out = _run(["workflow", "supervisor", "--status", "--home", self.home, "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["workspaces_total"], 0)
        self.assertEqual(payload["leases_held"], 0)

    def test_run_once_over_empty_home_supervises_nothing(self) -> None:
        rc, out = _run(
            ["workflow", "supervisor", "--run-once", "--home", self.home,
             "--holder", "superTest", "--json"]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "bounded_reconciliation")
        self.assertEqual(payload["workspaces_total"], 0)

    def test_run_once_with_wake_selects_local_wake_mode(self) -> None:
        rc, out = _run(
            ["workflow", "supervisor", "--run-once", "--home", self.home,
             "--wake", "wsA:13683", "--json"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["mode"], "local_wake")

    def test_no_action_is_rejected(self) -> None:
        parser = build_parser()
        # argparse's required mutually-exclusive group rejects a bare `workflow supervisor`.
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "supervisor"])


if __name__ == "__main__":
    unittest.main()
