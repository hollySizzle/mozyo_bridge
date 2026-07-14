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


# The explicit `--lane-spec` form (#13756). A verified delegated review lane, written the
# way a coordinator would write it from the durable record.
_DELEGATED_REVIEW_SPEC = (
    "issue=13441,state=review_waiting,"
    "actionability=delegated_in_flight,owner=dedicated_gateway,"
    "delivery=sent,callback_expected=true,"
    "surface=managed_sublane,workspace=w19,lane=issue_13441_provider_registry,"
    "revision=3,anchor=13441#77503,gateway=w19:p1,worker=w19:p2,ack=worker_confirmed"
)


class LaneSpecTest(unittest.TestCase):
    def test_delegated_review_lane_dispatches_next(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane-spec",
                _DELEGATED_REVIEW_SPEC,
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["fill_decision"], "dispatch_next")
        self.assertEqual(payload["delegated_in_flight"], ["13441"])
        self.assertEqual(payload["coordinator_blocking"], [])
        self.assertEqual(
            payload["capacity_projection"]["resident_managed_sublanes"], 1
        )

    def test_same_lane_without_the_explicit_claim_stops(self):
        # The compatibility contract: the legacy form makes no claim, so it still blocks.
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane",
                "13441:review_waiting",
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["fill_decision"], "stop_coordinator_blocking")
        self.assertEqual(payload["coordinator_blocking"], ["13441"])
        self.assertEqual(payload["delegated_in_flight"], [])

    def test_legacy_and_explicit_lanes_combine_into_one_lane_set(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane",
                "13682:implementing",
                "--lane-spec",
                _DELEGATED_REVIEW_SPEC,
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(payload["fill_decision"], "dispatch_next")
        self.assertEqual(payload["active_implementing"], ["13682"])
        self.assertEqual(payload["delegated_in_flight"], ["13441"])

    def test_delivery_failure_in_the_spec_stops(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane-spec",
                _DELEGATED_REVIEW_SPEC.replace("delivery=sent", "delivery=delivery_failed"),
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(payload["fill_decision"], "stop_coordinator_blocking")

    def test_unrecognized_vocabulary_value_fails_closed_rather_than_erroring(self):
        # A misread token must degrade to coordinator-blocking, not crash the command —
        # erroring would tempt the caller to drop the field entirely.
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane-spec",
                _DELEGATED_REVIEW_SPEC.replace(
                    "owner=dedicated_gateway", "owner=the_gateway"
                ),
                "--ready-independent",
                "1",
                "--capacity",
                "2",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["fill_decision"], "stop_coordinator_blocking")

    def test_task_agents_are_reported_apart_from_sublanes(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--lane-spec",
                "issue=1,state=implementing,surface=internal_task_agent",
                "--lane-spec",
                "issue=2,state=implementing,surface=internal_task_agent",
                "--ready-independent",
                "1",
                "--capacity",
                "1",
                "--sublane-hard-cap",
                "10",
                "--json",
            ]
        )
        payload = json.loads(text)
        projection = payload["capacity_projection"]
        self.assertEqual(rc, 0)
        self.assertEqual(projection["internal_task_agents"], 2)
        self.assertEqual(projection["resident_managed_sublanes"], 0)
        self.assertEqual(projection["worker_confirmed_productive_sublanes"], 0)
        # The task agents consumed no sublane capacity.
        self.assertEqual(payload["capacity_remaining"], 1)

    def test_actuation_unavailable_reports_the_fixed_blocked_result(self):
        rc, text = _run(
            [
                "workflow",
                "fill-decision",
                "--actuation-unavailable",
                "--ready-independent",
                "3",
                "--capacity",
                "5",
                "--json",
            ]
        )
        payload = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["fill_decision"], "stop_actuation_unavailable")
        self.assertFalse(payload["should_dispatch"])

    def test_text_output_renders_the_verified_projection(self):
        rc, text = _run(
            ["workflow", "fill-decision", "--lane-spec", _DELEGATED_REVIEW_SPEC]
        )
        self.assertEqual(rc, 0)
        self.assertIn("delegated_in_flight=['13441']", text)
        self.assertIn("resident=1", text)
        self.assertIn("worker_confirmed_productive=1", text)
        self.assertIn("internal_task_agents=0", text)


class MalformedLaneSpecTest(unittest.TestCase):
    def test_missing_issue_or_state_is_rejected(self):
        for spec in ("state=implementing", "issue=13756", ""):
            with self.subTest(spec=spec):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli_workflow_fill._parse_lane_spec(spec)

    def test_unknown_key_is_rejected(self):
        # Silently ignoring a misspelled key would turn a stalled delegation into a
        # healthy one.
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_fill._parse_lane_spec(
                "issue=1,state=implementing,callback_overdu=true"
            )

    def test_non_key_value_part_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_fill._parse_lane_spec("issue=1,state=implementing,overdue")

    def test_non_boolean_flag_value_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli_workflow_fill._parse_lane_spec(
                "issue=1,state=implementing,callback_overdue=maybe"
            )


if __name__ == "__main__":
    unittest.main()
