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

    def test_service_status_reports_projection_and_definition_exit_zero(self) -> None:
        # Patch the launchctl subprocess so no real host service is probed.
        with patch.object(sl.subprocess, "run", side_effect=_fake_run):
            rc, out = _run(["workflow", "supervisor", "--service-status", "--home", self.home, "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload["installed"])
        self.assertFalse(payload["loaded"])
        self.assertEqual(payload["phase"], "B1")
        self.assertFalse(payload["keep_alive_present"])
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        self.assertFalse(payload["definition"]["keep_alive"])
        # Secret-free and path-free.
        self.assertNotIn("api_key", out.lower())
        self.assertNotIn(self.home, out)

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
