"""`workflow supervisor` CLI tests (Redmine #13683 Phase A).

Pins the facade: run-once / status over a hermetic temp home, and the service lifecycle command
contract — service-status reports the secret-free definition (exit 0), while install / restart /
uninstall are fail-closed in Phase A (exit non-zero, NO host mutation).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    PHASE_A_SERVICE_MUTATION_REASON,
)


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

    def test_service_status_reports_definition_exit_zero(self) -> None:
        rc, out = _run(["workflow", "supervisor", "--service-status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload["installed"])
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        # Secret-free.
        self.assertNotIn("api_key", out.lower())

    def test_install_is_fail_closed_no_host_mutation(self) -> None:
        for verb in ("--install", "--restart", "--uninstall"):
            rc, out = _run(["workflow", "supervisor", verb, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["reason"], PHASE_A_SERVICE_MUTATION_REASON, verb)
            self.assertIn("no launchd/login service was changed", payload["note"].lower())

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
