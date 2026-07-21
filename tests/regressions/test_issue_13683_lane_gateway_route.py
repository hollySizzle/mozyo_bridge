"""Same-lane gateway routing for worker gates (Redmine #13683 R2, design answer j#82367).

A worker's ``implementation_done`` / ``review_request`` routes to the issue's OWN owning-lane
implementation_gateway (the same-lane Codex reviewer) on the distinct ``lane_gateway:<lane>`` route —
NOT the coordinator (the pre-R2 blanket route woke the coordinator, so the reviewing gateway stayed
``turn_ended``: installed a16 j#82329). Fail-closed on default/self/foreign/stale/no-owner/ambiguous/
no-gateway/blank-generation, and shadowed once the gateway has already reviewed (no spurious re-wake).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_DELIVERED, WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    discover_lane_gateway_sends,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (
    review_round_send_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    REVIEW_ROUND_CURRENT,
    REVIEW_ROUND_STALE,
    REVIEW_ROUND_UNVERIFIABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_gateway_route import (
    LANE_AMBIGUOUS_OWNER,
    LANE_BLANK_GENERATION,
    LANE_CURRENT_HEAD_UNCONFIRMED,
    LANE_NO_GATEWAY,
    LANE_NO_OWNER,
    LANE_PREVIOUS_GENERATION,
    LANE_SELF_ROUTE,
    LANE_SHADOWED,
    is_lane_gateway_route,
    lane_gateway_route,
    make_lane_gateway_send_edge_fence,
    plan_lane_gateway_sends,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    OwningLaneBinding,
)

ISSUE = "13683"
LANE = "issue_13683_lane"


def _owner(status=OWNER_RESOLVED, lane=LANE, generation="3", gateway="codex") -> OwningLaneBinding:
    return OwningLaneBinding(
        status=status, lane_id=lane, generation=generation, gateway_receiver=gateway
    )


def _req(journal="110"):
    return build_marker(ISSUE, journal, "review_request")


def _done(journal="100"):
    return build_marker(ISSUE, journal, "implementation_done")


def _result(journal="120"):
    return build_marker(ISSUE, journal, "review_result")


class PlanLaneGatewaySendsTest(unittest.TestCase):
    def _only(self, plans):
        self.assertEqual(len(plans), 1)
        return plans[0]

    def test_emits_to_owning_lane_gateway(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req("110")], ISSUE, _owner()))
        self.assertTrue(plan.emit)
        self.assertEqual(plan.callback_route, lane_gateway_route(LANE))
        self.assertEqual(plan.target_lane, LANE)
        self.assertEqual(plan.target_receiver, "codex")
        self.assertEqual(plan.target_generation, "3")
        self.assertEqual(plan.gate_journal, "110")

    def test_implementation_done_also_routes(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_done("100")], ISSUE, _owner()))
        self.assertTrue(plan.emit)
        self.assertEqual(plan.gate, "implementation_done")

    def test_self_route_when_owner_is_coordinator_lane(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req()], ISSUE, _owner(lane="default")))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_SELF_ROUTE)

    def test_no_owner_refused(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req()], ISSUE, _owner(status=OWNER_UNKNOWN)))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_NO_OWNER)

    def test_ambiguous_owner_refused(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req()], ISSUE, _owner(status=OWNER_AMBIGUOUS)))
        self.assertEqual(plan.reason, LANE_AMBIGUOUS_OWNER)

    def test_no_gateway_refused(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req()], ISSUE, _owner(gateway="")))
        self.assertEqual(plan.reason, LANE_NO_GATEWAY)

    def test_blank_generation_refused(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req()], ISSUE, _owner(generation="")))
        self.assertEqual(plan.reason, LANE_BLANK_GENERATION)

    def test_previous_generation_gate_fenced(self) -> None:
        # A gate on a journal OLDER than the current dispatch anchor is a previous-generation gate.
        plan = self._only(
            plan_lane_gateway_sends([_req("90")], ISSUE, _owner(), dispatch_anchor_journal="100")
        )
        self.assertEqual(plan.reason, LANE_PREVIOUS_GENERATION)

    def test_unresolvable_anchor_head_less_request_fails_closed(self) -> None:
        # Redmine #14094: an unresolvable anchor (a RESUMED lane with no fresh dispatch marker) no
        # longer blanket-fences as previous-generation. A head-LESS latest review_request looks current
        # (it IS the latest request) but its head is not a confirmable full head, so it fails closed
        # with the distinct current-head-unconfirmed diagnostic (still emit=False).
        plan = self._only(
            plan_lane_gateway_sends([_req("110")], ISSUE, _owner(), dispatch_anchor_journal="")
        )
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_CURRENT_HEAD_UNCONFIRMED)

    def test_current_generation_gate_emits_under_anchor(self) -> None:
        plan = self._only(
            plan_lane_gateway_sends([_req("110")], ISSUE, _owner(), dispatch_anchor_journal="100")
        )
        self.assertTrue(plan.emit)

    def test_shadowed_by_later_review_is_refused(self) -> None:
        # review_request j110 already answered by review_result j120 -> the gateway already reviewed.
        plans = plan_lane_gateway_sends([_req("110"), _result("120")], ISSUE, _owner())
        self.assertEqual(len(plans), 1)  # only the worker gate is planned (review_result is not a worker gate)
        self.assertFalse(plans[0].emit)
        self.assertEqual(plans[0].reason, LANE_SHADOWED)

    def test_fresh_rework_request_after_review_is_unshadowed(self) -> None:
        # review_request j110 -> review_result j120 -> a NEW review_request j130 (re-work) is unshadowed.
        plans = plan_lane_gateway_sends([_req("110"), _result("120"), _req("130")], ISSUE, _owner())
        emits = [p for p in plans if p.emit]
        self.assertEqual([p.gate_journal for p in emits], ["130"])

    def test_unreviewed_request_emits(self) -> None:
        plan = self._only(plan_lane_gateway_sends([_req("110")], ISSUE, _owner()))
        self.assertTrue(plan.emit)


class LaneGatewaySendEdgeFenceTest(unittest.TestCase):
    class _Row:
        def __init__(self, route, journal):
            self.callback_route = route
            self.journal = journal

    def test_fences_previous_generation_lane_gateway_row(self) -> None:
        fence = make_lane_gateway_send_edge_fence("100")
        fenced, reason = fence(self._Row(lane_gateway_route(LANE), "90"))
        self.assertTrue(fenced)
        self.assertIn("superseded", reason)

    def test_current_lane_gateway_row_passes(self) -> None:
        fence = make_lane_gateway_send_edge_fence("100")
        self.assertEqual(fence(self._Row(lane_gateway_route(LANE), "110")), (False, ""))

    def test_non_lane_gateway_row_exempt(self) -> None:
        fence = make_lane_gateway_send_edge_fence("100")
        self.assertEqual(fence(self._Row("coordinator", "90")), (False, ""))

    def test_unresolvable_anchor_fences(self) -> None:
        fence = make_lane_gateway_send_edge_fence("")
        fenced, reason = fence(self._Row(lane_gateway_route(LANE), "110"))
        self.assertTrue(fenced)
        self.assertIn("unresolvable", reason)


class LaneGatewayActionTimeShadowFenceTest(unittest.TestCase):
    """Send-edge shadow re-check (review j#82382 F1): a row pending BEFORE the review terminates once
    the review lands, so a later backlog drain never re-wakes the gateway; a rework gate still sends."""

    class _Row:
        def __init__(self, journal):
            self.callback_route = lane_gateway_route(LANE)
            self.issue = ISSUE
            self.journal = journal

    def _reviewed_source(self):
        # review_request j110 -> review_result j120 (the gateway already reviewed j110).
        return MappingRedmineJournalSource(
            payload={
                "issue": {"id": ISSUE},
                "journals": [
                    {"id": "110", "notes": "[mozyo:workflow-event:gate=review_request:conclusion=pending]"},
                    {"id": "120", "notes": "[mozyo:workflow-event:gate=review_result:conclusion=approved]"},
                ],
            }
        )

    def test_shadowed_row_is_terminal_at_send_edge(self) -> None:
        fence = review_round_send_fence(self._reviewed_source)
        self.assertEqual(fence(self._Row("110")), REVIEW_ROUND_STALE)

    def test_rework_gate_newer_than_review_still_sends(self) -> None:
        fence = review_round_send_fence(self._reviewed_source)
        self.assertEqual(fence(self._Row("130")), REVIEW_ROUND_CURRENT)

    def test_unreviewed_gate_sends(self) -> None:
        source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": ISSUE},
                "journals": [
                    {"id": "110", "notes": "[mozyo:workflow-event:gate=review_request:conclusion=pending]"}
                ],
            }
        )
        fence = review_round_send_fence(lambda: source)
        self.assertEqual(fence(self._Row("110")), REVIEW_ROUND_CURRENT)

    def test_unreadable_source_is_retryable_not_terminal(self) -> None:
        fence = review_round_send_fence(lambda: None)
        self.assertEqual(fence(self._Row("110")), REVIEW_ROUND_UNVERIFIABLE)

    def test_blank_identity_is_terminal(self) -> None:
        fence = review_round_send_fence(self._reviewed_source)
        row = self._Row("")
        self.assertEqual(fence(row), REVIEW_ROUND_STALE)


class DiscoverLaneGatewaySendsTest(unittest.TestCase):
    def test_candidate_carries_route_and_target(self) -> None:
        source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": ISSUE},
                "journals": [
                    {"id": "110", "notes": "[mozyo:workflow-event:gate=review_request:conclusion=pending]"}
                ],
            }
        )
        candidates, plans = discover_lane_gateway_sends(source, ISSUE, _owner(), workspace_id="wsA")
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertTrue(is_lane_gateway_route(c.callback_route))
        self.assertEqual(c.target_lane, LANE)
        self.assertEqual(c.target_receiver, "codex")
        self.assertEqual(c.target_generation, "3")
        self.assertEqual(c.workspace_id, "wsA")
        self.assertEqual(c.journal, "110")


class SupervisorLaneGatewayScenarioTest(unittest.TestCase):
    """The worker review_request routes to the owning-lane gateway, not the coordinator (delivered)."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": ISSUE},
                "journals": [
                    {"id": "110", "notes": "[mozyo:workflow-event:gate=review_request:conclusion=pending]"}
                ],
            }
        )
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def test_review_request_delivers_to_lane_gateway(self) -> None:
        sent: list = []

        def sender(row):
            sent.append(row)
            return SEND_DELIVERED

        sup = WorkspaceCallbackSupervisor(
            holder="superX",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=lambda ws: self.source,
            sender_fn=lambda ws: sender,
            # The owner-binding fn activates the lane_gateway route (and the coordinator exclusion).
            owner_binding_fn=lambda wsid, issue, binding: _owner(),
            clock=lambda: "2026-07-19T00:00:00+00:00",
        )
        report = sup.run_once()
        # The review_request was routed to the owning-lane gateway (a lane_gateway row), delivered.
        self.assertEqual(len(sent), 1)
        self.assertTrue(is_lane_gateway_route(sent[0].callback_route))
        self.assertEqual(sent[0].target_receiver, "codex")
        self.assertEqual(sent[0].target_lane, LANE)
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual(len(delivered), 1)
        self.assertTrue(is_lane_gateway_route(delivered[0].callback_route))
        # It did NOT also produce a coordinator-route row (no double wake).
        self.assertEqual(
            [r for r in self.outbox.read() if r.callback_route == "coordinator"], []
        )
        self.assertEqual(report.workspaces[0].delivered, 1)


if __name__ == "__main__":
    unittest.main()
