"""`workflow runtime` CLI integration tests (Redmine #12857).

Covers the stateful runtime command surface (first vertical slice):

- the subcommand registers under the ``workflow`` family;
- ``--event`` replays a durable event log; an ``implementing``-only set with ready work +
  capacity reports ``dispatch_next_sublane`` and returns 0 (the active-implementing-lane-
  is-not-a-stop invariant, end to end);
- a repeated ``id=`` is suppressed (replay idempotency is observable);
- a ``review_request`` lane drives the concrete next action ``perform_review`` (still exit
  0: advisory);
- ``--json`` carries the enriched nested ``workflow.{state,next_action}`` envelope (#12671
  j#68908 finding 2: the runtime command result includes route_identity / anchor /
  risk_level / requires_confirmation / blocked_reason); ``--route-identity`` resolves the
  next action's route; ``--journal`` emits the durable record markdown;
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


class IdOmissionRegressionTest(unittest.TestCase):
    """Review Gate j#68580 finding 1: omitting ``id=`` must not collapse distinct events.

    Two genuinely-distinct journal events for the same issue + gate (different facts) must
    both apply — last-applied-event-per-issue wins — instead of the later one being
    falsely suppressed as a duplicate.
    """

    def test_same_issue_same_gate_different_facts_without_id_both_apply(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:review,conclusion=pending",
                "--event",
                "12857:review,conclusion=approved",
            ]
        )
        self.assertEqual(rc, 0)
        # The later approved review wins -> owner aggregation, NOT review_waiting.
        self.assertIn("next_action: aggregate_owner_approval", text)
        self.assertIn("12857 -> owner_waiting", text)
        self.assertIn("suppressed=<none>", text)

    def test_same_issue_same_gate_without_id_json_reflects_latest(self):
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:review,conclusion=pending",
                "--event",
                "12857:review,conclusion=approved",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        wf = json.loads(text)["workflow"]
        self.assertEqual(wf["state"]["suppressed_event_ids"], [])
        self.assertEqual(len(wf["state"]["applied_event_ids"]), 2)
        self.assertEqual(
            wf["next_action"]["action"], "aggregate_owner_approval"
        )
        self.assertEqual(
            wf["state"]["lane_actions"][0]["state_class"], "owner_waiting"
        )

    def test_explicit_shared_id_still_suppressed(self):
        # The duplicate-suppression feature stays intact for a real shared durable anchor.
        rc, text = _run(
            [
                "workflow",
                "runtime",
                "--event",
                "12857:review_request,id=12857:j1",
                "--event",
                "12857:review_request,id=12857:j1",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("applied=['12857:j1']", text)
        self.assertIn("suppressed=['12857:j1']", text)


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
        self.assertIn("workflow", payload)
        wf = payload["workflow"]
        self.assertTrue(wf["advisory"])
        self.assertEqual(
            wf["next_action"]["action"], "aggregate_owner_approval"
        )
        self.assertEqual(wf["next_action"]["target_issue"], "12857")
        # enriched safety fields are present on the runtime command result (j#68908 finding 2)
        for key in ("route_identity", "anchor", "risk_level", "requires_confirmation", "blocked_reason"):
            self.assertIn(key, wf["next_action"])
        self.assertEqual(wf["state"]["applied_event_ids"], ["r"])
        self.assertIn("admission", wf["state"])

    def test_route_identity_resolves_and_omits_pane_id(self):
        # A supplied --route-identity (with a pane_id cache) resolves the next action's
        # route via the owner_role provider match; the pane id never appears in the JSON.
        rc, text = _run(
            [
                "workflow", "runtime",
                "--event", "12857:review_request,id=12857:j1,commit=1",
                "--route-identity", "route_id=r,issue=12857,ws=ws1,role=codex,pane_name=gw,pane_id=%17",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        na = json.loads(text)["workflow"]["next_action"]
        self.assertEqual(na["action"], "perform_review")
        self.assertIn("pane_name=gw", na["route_identity"])
        self.assertEqual(na["blocked_reason"], "")
        self.assertNotIn("%17", text)

    def test_route_unresolved_fails_closed(self):
        rc, text = _run(
            [
                "workflow", "runtime",
                "--event", "12857:review_request,id=12857:j1,commit=1",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        na = json.loads(text)["workflow"]["next_action"]
        self.assertEqual(na["blocked_reason"], "route_identity_unresolved")
        self.assertTrue(na["requires_confirmation"])


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
