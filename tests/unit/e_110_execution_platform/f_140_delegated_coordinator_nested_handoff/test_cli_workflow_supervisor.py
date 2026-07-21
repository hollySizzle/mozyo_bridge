"""`workflow supervisor` CLI tests (Redmine #13683 Phase B1).

Pins the facade: run-once / status over a hermetic temp home, and the service lifecycle command
contract — service-status is a redacted projection + secret-free definition (exit 0), while
install / restart / uninstall drive the owned LaunchAgent and fail-closed (exit non-zero, zero
mutation) on a non-darwin host. Real launchctl is never invoked here (patched).
"""

from __future__ import annotations

import contextlib
import io
import json
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


class CliWorkflowSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))
        # A hermetic OS user home for the owned LaunchAgent plist/log, so the
        # projection never reads the dogfood host's real ~/Library/LaunchAgents
        # (Redmine #14103). --home is the mozyo home and by design never
        # relocates the plist, so the plist root is isolated via Path.home().
        self.os_home = Path(tempfile.mkdtemp())

    def _service_status(self) -> tuple[int, str]:
        """Run ``--service-status`` with both host reads isolated: launchctl is a
        fake subprocess and ``Path.home()`` resolves to a temp root, so
        ``installed`` reflects only the plist this test controls under
        ``self.os_home`` — never the host's installed service."""
        with patch.object(sl.subprocess, "run", side_effect=_fake_run), patch(
            "pathlib.Path.home", return_value=self.os_home
        ):
            return _run(["workflow", "supervisor", "--service-status", "--home", self.home, "--json"])

    def test_service_status_reports_projection_and_definition_exit_zero(self) -> None:
        rc, out = self._service_status()
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        # Redmine #14150: the projection is now the owned PAIR (reconcile + drain agents).
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
        rc, out = self._service_status()
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        reconcile = payload["agents"][0]
        self.assertTrue(reconcile["installed"])  # the reconcile agent's owned plist is present
        self.assertTrue(reconcile["plist_exists"])
        # The drain agent was NOT installed, so the pair projection distinguishes them.
        self.assertFalse(payload["agents"][1]["installed"])

    def test_mutating_verbs_fail_closed_zero_mutation_on_non_darwin(self) -> None:
        with patch.object(sl, "_running_on_darwin", return_value=False), patch.object(
            sl.subprocess, "run", side_effect=AssertionError("launchctl must not run")
        ):
            for verb in ("--install", "--restart", "--uninstall"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
                payload = json.loads(out)
                self.assertEqual(rc, 1, verb)
                self.assertFalse(payload["performed"], verb)
                self.assertEqual(payload["reason"], sl.REASON_UNSUPPORTED_PLATFORM, verb)

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
