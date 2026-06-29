"""`workflow fill-decision` CLI integration tests (Redmine #12855).

Covers the advisory command surface:

- the subcommand registers under the ``workflow`` family;
- ``--json`` emits exactly one structured envelope and the command always returns 0
  (advisory: it never blocks a handoff);
- an ``implementing``-only lane set with ready work + capacity reports
  ``dispatch_next`` (the active-implementing-lane-is-not-a-stop invariant, end to end);
- a coordinator-blocking lane reports the concrete stop reason;
- ``--lane`` rejects a malformed spec.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_fill,
)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_fill_decision_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "fill-decision", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_fill_decision")
        self.assertTrue(ns.as_json)


class DispatchNextTest(unittest.TestCase):
    def test_implementing_only_dispatches_next_and_returns_zero(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane",
                "12855:implementing",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)  # exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["fill_decision"], "dispatch_next")
        self.assertTrue(payload["advisory"])
        self.assertEqual(payload["active_implementing"], ["12855"])


class StopReasonTest(unittest.TestCase):
    def test_coordinator_blocking_reports_concrete_stop_reason(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane",
                "12855:implementing",
                "--lane",
                "12700:review_waiting",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
            ]
        )
        # Advisory: never blocks, so rc is 0 even on a stop decision.
        self.assertEqual(rc, 0)
        self.assertIn("fill_decision: stop_coordinator_blocking", text)
        self.assertIn("next_drain_action: review", text)

    def test_owner_or_release_gate_reports_stop(self):
        rc, text = _run(
            ["workflow", "fill-decision", "--owner-or-release-gate"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("fill_decision: stop_owner_or_release_gate", text)


class MalformedLaneTest(unittest.TestCase):
    def test_lane_without_state_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_fill._parse_lane("12855")

    def test_lane_parser_via_cli_exits(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["workflow", "fill-decision", "--lane", "noseparator"])


if __name__ == "__main__":
    unittest.main()
