"""`workflow runtime` CLI integration tests (Redmine #12857).

Covers the stateful runtime command surface (first vertical slice):

- the subcommand registers under the ``workflow`` family;
- ``--event`` replays a durable event log; an ``implementing``-only set with ready work +
  capacity reports ``dispatch_next_sublane`` and returns 0 (the active-implementing-lane-
  is-not-a-stop invariant, end to end);
- a repeated ``id=`` is suppressed (replay idempotency is observable);
- a ``review_request`` lane drives the concrete next action ``perform_review`` (still exit
  0: advisory);
- ``--json`` carries ``workflow.state`` + ``workflow.next_action``; ``--journal`` emits the
  durable record markdown;
- ``--event`` rejects a malformed spec / unknown gate / empty id.
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


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_runtime_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "runtime", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_runtime")
        self.assertTrue(ns.as_json)


class DispatchTest(unittest.TestCase):
    def test_implementing_only_dispatches_and_returns_zero(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:start",
                "--ready-independent",
                "1",
                "--capacity",
                "1",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("next_action: dispatch_next_sublane", text)
        self.assertIn("owner_role: coordinator", text)
        self.assertIn("12857 -> implementing", text)


class DuplicateSuppressionTest(unittest.TestCase):
    def test_repeated_event_id_is_suppressed(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:review_request,id=12857:68580",
                "--event",
                "12857:review_request,id=12857:68580",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("applied=['12857:68580']", text)
        self.assertIn("suppressed=['12857:68580']", text)
        self.assertIn("next_action: perform_review", text)
        self.assertIn("owner_role: auditor", text)


class JsonTest(unittest.TestCase):
    def test_json_carries_state_and_next_action(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:review,id=r,conclusion=approved",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        self.assertTrue(payload["advisory"])
        self.assertEqual(
            payload["next_action"]["action"], "aggregate_owner_approval"
        )
        self.assertEqual(payload["next_action"]["target_issue"], "12857")
        self.assertEqual(payload["state"]["applied_event_ids"], ["r"])
        self.assertIn("admission", payload["state"])


class JournalTest(unittest.TestCase):
    def test_journal_render(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12858:close,id=c,commit=1",
                "--journal",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("## Sublane dispatch decision", text)
        self.assertIn("## Workflow runtime next action", text)
        self.assertIn("- next_action: integrate", text)
        self.assertIn("- target_issue: 12858", text)


class ParseErrorTest(unittest.TestCase):
    def test_malformed_event_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["workflow", "runtime", "--event", "noseparator"])

    def test_unknown_gate_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["workflow", "runtime", "--event", "12857:not_a_gate"]
            )

    def test_empty_id_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["workflow", "runtime", "--event", "12857:start,id="]
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
