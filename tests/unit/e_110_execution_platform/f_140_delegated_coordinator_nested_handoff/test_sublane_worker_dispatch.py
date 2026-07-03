"""Pure worker-dispatch ack-drive VO / renderer tests (Redmine #12988).

Covers the pure domain surface with no IO: the request identity guard, the
j#70250-style lane-identity match, the outcome payload shape (confirmed only on
the explicit ``worker_dispatched`` token), and the durable-record renderer for
the executed / dry-run / blocked variants — including the fail-closed
``gateway_notified`` next-action language.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_NOT_ATTEMPTED,
    DISPATCH_WORKER_DISPATCHED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneLaneView,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    REASON_WORKER_DISPATCH_FAILED,
    WORKER_DISPATCH_DELIVERY_FAILED,
    WORKER_DISPATCH_RESULTS,
    WorkerDispatchOutcome,
    WorkerDispatchRequest,
    lane_identity_matches,
    render_worker_dispatch_journal,
)


def _lane(**kw):
    base = dict(
        workspace_id="ws",
        lane_id="l1",
        lane_label="issue_12988_x",
        issue="12988",
        branch="b",
        repo_root="/wt/12988",
        gateway_pane="%176",
        worker_pane="%177",
        state="active",
    )
    base.update(kw)
    return SublaneLaneView(**base)


def _outcome(**kw):
    base = dict(
        status=ACTUATE_EXECUTED,
        execute=True,
        reason="ok",
        issue="12988",
        lane_label="issue_12988_x",
        worktree_path="/wt/12988",
        gateway_pane="%176",
        worker_pane="%177",
        dispatch_target="%177",
        dispatch_result=DISPATCH_WORKER_DISPATCHED,
        durable_anchor="71524",
        command="mozyo-bridge handoff send --to claude ...",
    )
    base.update(kw)
    return WorkerDispatchOutcome(**base)


class RequestTests(unittest.TestCase):
    def test_missing_fields_names_each_blank_identity_field(self):
        request = WorkerDispatchRequest(issue="", lane_label=" ", worktree_path="")
        self.assertEqual(
            request.missing_fields(), ("issue", "lane_label", "worktree_path")
        )

    def test_complete_request_has_no_missing_fields(self):
        request = WorkerDispatchRequest(
            issue="12988", lane_label="issue_12988_x", worktree_path="/wt/12988"
        )
        self.assertEqual(request.missing_fields(), ())


class LaneIdentityMatchTests(unittest.TestCase):
    def test_matching_label_and_issue_passes(self):
        self.assertTrue(
            lane_identity_matches(_lane(), issue="12988", lane_label="issue_12988_x")
        )

    def test_label_mismatch_fails_closed(self):
        self.assertFalse(
            lane_identity_matches(_lane(), issue="12988", lane_label="issue_12989_y")
        )

    def test_issue_mismatch_fails_closed(self):
        self.assertFalse(
            lane_identity_matches(
                _lane(issue="12989"), issue="12988", lane_label="issue_12988_x"
            )
        )

    def test_blank_requested_label_fails_closed(self):
        self.assertFalse(lane_identity_matches(_lane(), issue="12988", lane_label=""))

    def test_unpopulated_lane_issue_is_reparsed_from_label(self):
        # A lane whose `issue` field was not pre-populated is still validated by
        # re-parsing its label (the j#70250 guard's fallback).
        self.assertTrue(
            lane_identity_matches(
                _lane(issue=None), issue="12988", lane_label="issue_12988_x"
            )
        )
        self.assertFalse(
            lane_identity_matches(
                _lane(issue=None), issue="12989", lane_label="issue_12988_x"
            )
        )


class OutcomeTests(unittest.TestCase):
    def test_result_vocabulary_is_the_three_drive_literals(self):
        self.assertEqual(
            WORKER_DISPATCH_RESULTS,
            {
                DISPATCH_WORKER_DISPATCHED,
                WORKER_DISPATCH_DELIVERY_FAILED,
                DISPATCH_NOT_ATTEMPTED,
            },
        )

    def test_confirmed_only_on_worker_dispatched(self):
        # #12988 acceptance: no false promotion — only the explicit
        # worker_dispatched token confirms; delivery_failed / not_attempted stay
        # unconfirmed.
        self.assertTrue(_outcome().worker_dispatch_confirmed)
        self.assertFalse(
            _outcome(
                dispatch_result=WORKER_DISPATCH_DELIVERY_FAILED
            ).worker_dispatch_confirmed
        )
        self.assertFalse(
            _outcome(dispatch_result=DISPATCH_NOT_ATTEMPTED).worker_dispatch_confirmed
        )

    def test_payload_round_trips_fields(self):
        payload = _outcome().as_payload()
        self.assertEqual(payload["status"], ACTUATE_EXECUTED)
        self.assertEqual(payload["dispatch_result"], "worker_dispatched")
        self.assertTrue(payload["worker_dispatch_confirmed"])
        self.assertEqual(payload["gateway_pane"], "%176")
        self.assertEqual(payload["worker_pane"], "%177")
        self.assertEqual(payload["durable_anchor"], "71524")
        self.assertIn("command", payload)
        self.assertEqual(payload["blocked_reasons"], [])


class RenderTests(unittest.TestCase):
    def test_executed_render_confirms_but_stays_delivery_ack_only(self):
        text = render_worker_dispatch_journal(_outcome())
        self.assertIn("## sublane worker dispatched", text)
        self.assertIn("- dispatch_result: worker_dispatched", text)
        self.assertIn("- worker_dispatch_confirmed: true", text)
        # The confirmed record must not read as worker progress / completion.
        self.assertIn("delivery ACK only", text)
        self.assertIn("not worker progress or completion", text)

    def test_delivery_failed_render_keeps_gateway_notified_semantics(self):
        text = render_worker_dispatch_journal(
            _outcome(
                status=ACTUATE_BLOCKED,
                dispatch_result=WORKER_DISPATCH_DELIVERY_FAILED,
                blocked_reasons=(REASON_WORKER_DISPATCH_FAILED,),
            )
        )
        self.assertIn("## sublane worker dispatch blocked", text)
        self.assertIn("- dispatch_result: delivery_failed", text)
        self.assertIn("- worker_dispatch_confirmed: false", text)
        self.assertIn("`gateway_notified`", text)
        self.assertIn("fail-closed", text)
        self.assertIn("- blocked_reasons: worker_dispatch_failed", text)
        # Recovery pointers stay replayable.
        self.assertIn("callback-recovery", text)
        self.assertIn("- command:", text)

    def test_dry_run_render_points_at_execute(self):
        text = render_worker_dispatch_journal(
            _outcome(
                status=ACTUATE_READY,
                execute=False,
                dispatch_result=DISPATCH_NOT_ATTEMPTED,
            )
        )
        self.assertIn("## sublane worker dispatch plan (dry-run)", text)
        self.assertIn("re-run with --execute", text)
        self.assertIn("- worker_dispatch_confirmed: false", text)


if __name__ == "__main__":
    unittest.main()
