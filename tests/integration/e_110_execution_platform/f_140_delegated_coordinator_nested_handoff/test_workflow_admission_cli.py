"""`workflow admission` CLI integration tests (Redmine #12856).

Covers the Redmine-aware advisory command surface:

- the subcommand registers under the ``workflow`` family;
- ``--lane-signal`` classifies durable-record facts, and an ``implementing``-only set
  with ready work + capacity reports ``dispatch_sublane`` / ``dispatch_next`` and
  returns 0 (the active-implementing-lane-is-not-a-stop invariant, end to end);
- a ``review_request`` lane reports the concrete stop reason (still exit 0: advisory);
- ``--journal`` emits the Bandwidth Record Template markdown;
- ``--lane-signal`` rejects a malformed spec / unknown gate.
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
    cli_workflow_admission,
)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_admission_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "admission", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_admission")
        self.assertTrue(ns.as_json)


class DispatchTest(unittest.TestCase):
    def test_implementing_only_dispatches_and_returns_zero(self):
        rc, text = _run(
            [
                "workflow",
                "admission",
                "--lane-signal",
                "12856:start",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)  # exactly one JSON object
        self.assertEqual(rc, 0)
        self.assertEqual(payload["admission_decision"], "dispatch_sublane")
        self.assertEqual(payload["fill"]["fill_decision"], "dispatch_next")
        self.assertTrue(payload["advisory"])
        self.assertEqual(
            payload["classified_lanes"],
            [{"issue": "12856", "state_class": "implementing"}],
        )


class StopReasonTest(unittest.TestCase):
    def test_review_request_lane_reports_stop_and_drain(self):
        rc, text = _run(
            [
                "workflow",
                "admission",
                "--lane-signal",
                "12856:start",
                "--lane-signal",
                "12700:review_request",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
            ]
        )
        # Advisory: never blocks, so rc is 0 even on a stop decision.
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: stop_and_drain", text)
        self.assertIn("fill_decision: stop_coordinator_blocking", text)
        self.assertIn("12700 -> review_waiting", text)
        self.assertIn("next_drain_action: review", text)

    def test_review_changes_requested_is_not_a_stop(self):
        # A review returning changes is back-to-implementer, not coordinator-blocking.
        rc, text = _run(
            [
                "workflow",
                "admission",
                "--lane-signal",
                "12856:review,conclusion=changes_requested",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["admission_decision"], "dispatch_sublane")
        self.assertEqual(
            payload["classified_lanes"][0]["state_class"], "implementing"
        )


class JournalTest(unittest.TestCase):
    def test_journal_emits_template(self):
        rc, text = _run(
            [
                "workflow",
                "admission",
                "--lane-signal",
                "12856:owner_close_approval,commit=1,integrated=0",
                "--journal",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("## Sublane dispatch decision", text)
        self.assertIn("12856: integration_waiting", text)
        self.assertIn("admission_decision: stop_and_drain", text)


class MalformedSignalTest(unittest.TestCase):
    def test_signal_without_gate_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_admission._parse_lane_signal("12856")

    def test_unknown_gate_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_admission._parse_lane_signal("12856:not_a_gate")

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_admission._parse_lane_signal("12856:start,bogus=1")

    def test_signal_parser_via_cli_exits(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(
                    ["workflow", "admission", "--lane-signal", "noseparator"]
                )


if __name__ == "__main__":
    unittest.main()
