"""Current-generation callback selection for a resumed correction lane (Redmine #14094).

A RESUMED correction lane opens a fresh generation WITHOUT a new ``implementation_request`` marker, so
its IR dispatch anchor is UNRESOLVABLE. Before #14094 the callback supervisor blanket-classified the
lane's valid full-head Review Request as ``previous_generation_gate`` and 0-sent it, even though it is
the current gate (installed production 0.12.0: events supplied 3, delivery 0, pending 0 — the #13948
j#82690 reproduction). The fix selects the current gate by combining the current active owning-lane
binding (foreign / non-current lifecycle fail-closed elsewhere), the current decision anchor, and the
exact ``review_request:head=<full SHA>``: the latest full-head review_request IS the current gate even
when the IR anchor is unresolvable, while previous / malformed-or-missing head / already-completed /
older-than-a-resolvable-anchor gates stay 0-send with a diagnostic that separates a historical fence
from a "looks current but unconfirmable head" refusal. No prose SHA is ever guessed.
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
    build_supervisor_send_edge_fence,
    resolve_current_request_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_gateway_route import (
    LANE_CURRENT_HEAD_UNCONFIRMED,
    LANE_PREVIOUS_GENERATION,
    LANE_SHADOWED,
    current_review_request_journal,
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
    OWNER_RESOLVED,
    OwningLaneBinding,
)

ISSUE = "14094"
LANE = "issue_14094_current_generation_callback_r1"

# Two distinct FULL commit heads (40 hex chars). HEAD_CURR is the #13948 j#82690 head from the issue.
HEAD_CURR = "bdd8bce4f38e94850267d5b017d524fe47423d7d"
HEAD_PREV = "a1382f92deadbeefcafef00d1234567890abcdef"


def _owner(status=OWNER_RESOLVED, lane=LANE, generation="3", gateway="codex") -> OwningLaneBinding:
    return OwningLaneBinding(
        status=status, lane_id=lane, generation=generation, gateway_receiver=gateway
    )


def _req(journal, head=HEAD_CURR):
    return build_marker(ISSUE, journal, "review_request", target_head=head)


def _result(journal, head=HEAD_PREV, req="", conclusion="changes_requested"):
    return build_marker(
        ISSUE, journal, "review_result",
        target_head=head, review_request_journal=req, review_conclusion=conclusion,
    )


class ResumedLaneCurrentGatePlanTest(unittest.TestCase):
    """The pure discovery policy under an UNRESOLVABLE anchor (a resumed correction lane)."""

    def _plans(self, markers, anchor=""):
        return plan_lane_gateway_sends(markers, ISSUE, _owner(), dispatch_anchor_journal=anchor)

    def _only(self, plans):
        self.assertEqual(len(plans), 1)
        return plans[0]

    def test_current_full_head_request_emits_despite_unresolvable_anchor(self) -> None:
        # The #13948 j#82690 reproduction: a resumed lane's fresh generation carries no new IR marker
        # (anchor unresolvable), yet its latest full-head Review Request IS the current gate and sends.
        plan = self._only(self._plans([_req("82690", HEAD_CURR)], anchor=""))
        self.assertTrue(plan.emit)
        self.assertEqual(plan.callback_route, lane_gateway_route(LANE))
        self.assertEqual(plan.gate_journal, "82690")

    def test_multi_generation_selects_only_the_current_request(self) -> None:
        # A previous review generation (older request) plus the current corrected request; only the
        # current full-head request emits, the older one is a historical previous-generation fence.
        plans = self._plans(
            [_req("82600", HEAD_PREV), _req("82690", HEAD_CURR)], anchor=""
        )
        emits = [p for p in plans if p.emit]
        self.assertEqual([p.gate_journal for p in emits], ["82690"])
        older = next(p for p in plans if p.gate_journal == "82600")
        self.assertFalse(older.emit)
        self.assertEqual(older.reason, LANE_PREVIOUS_GENERATION)

    def test_malformed_current_head_is_current_head_unconfirmed(self) -> None:
        # The latest request LOOKS current but its head is not a full commit head -> a distinct
        # fail-closed diagnostic (never conflated with a historical fence, never a prose SHA guess).
        plan = self._only(self._plans([_req("82690", "bdd8bce4")], anchor=""))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_CURRENT_HEAD_UNCONFIRMED)

    def test_missing_head_is_current_head_unconfirmed(self) -> None:
        plan = self._only(self._plans([build_marker(ISSUE, "82690", "review_request")], anchor=""))
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_CURRENT_HEAD_UNCONFIRMED)

    def test_ambiguous_current_head_is_current_head_unconfirmed(self) -> None:
        # Two review_request markers on the SAME latest journal disagreeing on head -> the current head
        # authority is ambiguous -> unconfirmable (not an emit).
        plans = self._plans([_req("82690", HEAD_CURR), _req("82690", HEAD_PREV)], anchor="")
        self.assertTrue(all(not p.emit for p in plans))
        self.assertTrue(all(p.reason == LANE_CURRENT_HEAD_UNCONFIRMED for p in plans))

    def test_already_completed_same_head_stays_shadowed(self) -> None:
        # The current request was already answered by a later review_result -> the gateway already
        # reviewed it; re-waking would be a spurious duplicate. Stays 0-send (shadowed).
        plans = self._plans(
            [_req("82690", HEAD_CURR), _result("82700", HEAD_CURR, req="82690", conclusion="approved")],
            anchor="",
        )
        self.assertEqual(len(plans), 1)  # only the worker gate is planned
        self.assertFalse(plans[0].emit)
        self.assertEqual(plans[0].reason, LANE_SHADOWED)

    def test_implementation_done_stays_fail_closed_under_unresolvable_anchor(self) -> None:
        # Only a review_request is pinned by the review generation head; an implementation_done gate on
        # an unresolvable anchor stays fail-closed (a historical fence), never rescued.
        plan = self._only(
            self._plans([build_marker(ISSUE, "82690", "implementation_done")], anchor="")
        )
        self.assertFalse(plan.emit)
        self.assertEqual(plan.reason, LANE_PREVIOUS_GENERATION)


class ResolvableAnchorNoRegressionTest(unittest.TestCase):
    """A RESOLVABLE anchor keeps the strict older-than-anchor generation fence (no #14094 regression)."""

    def test_current_request_at_or_after_anchor_emits(self) -> None:
        plans = plan_lane_gateway_sends(
            [_req("82690", HEAD_CURR)], ISSUE, _owner(), dispatch_anchor_journal="82600"
        )
        self.assertTrue(plans[0].emit)

    def test_request_before_resolvable_anchor_is_previous_generation(self) -> None:
        # Even a full-head latest request that predates a RESOLVABLE fresh dispatch is a previous
        # generation (the current generation opened after it and has not re-requested yet).
        plans = plan_lane_gateway_sends(
            [_req("82600", HEAD_CURR)], ISSUE, _owner(), dispatch_anchor_journal="82650"
        )
        self.assertFalse(plans[0].emit)
        self.assertEqual(plans[0].reason, LANE_PREVIOUS_GENERATION)


class CurrentRequestJournalHelperTest(unittest.TestCase):
    def test_returns_latest_full_head_request_journal(self) -> None:
        markers = [_req("82600", HEAD_PREV), _req("82690", HEAD_CURR)]
        self.assertEqual(current_review_request_journal(markers, ISSUE), "82690")

    def test_blank_when_latest_head_malformed(self) -> None:
        self.assertEqual(current_review_request_journal([_req("82690", "bdd8bce4")], ISSUE), "")

    def test_blank_when_no_request(self) -> None:
        self.assertEqual(current_review_request_journal([], ISSUE), "")


class SendEdgeFenceResumedLaneTest(unittest.TestCase):
    """The send-edge fence exempts the resumed-lane current-gate row on an unresolvable anchor."""

    class _Row:
        def __init__(self, journal, route=None, normalized_gate="review_request"):
            self.callback_route = route or lane_gateway_route(LANE)
            self.journal = journal
            self.normalized_gate = normalized_gate

    def test_current_request_row_exempt_under_unresolvable_anchor(self) -> None:
        fence = make_lane_gateway_send_edge_fence("", current_request_journal="82690")
        self.assertEqual(fence(self._Row("82690")), (False, ""))

    def test_non_current_row_still_fenced_under_unresolvable_anchor(self) -> None:
        fence = make_lane_gateway_send_edge_fence("", current_request_journal="82690")
        fenced, reason = fence(self._Row("82600"))
        self.assertTrue(fenced)
        self.assertIn("unresolvable", reason)

    def test_same_journal_implementation_done_row_stays_fenced(self) -> None:
        # Review j#82729 F1: the exemption conjoins the gate kind. A same-journal implementation_done
        # row (a combined Impl Done / Review Request journal, or a pre-existing backlog row) must stay
        # fenced — matching on journal alone would wrongly exempt a gate discovery 0-sends.
        fence = make_lane_gateway_send_edge_fence("", current_request_journal="82690")
        exempt = fence(self._Row("82690", normalized_gate="review_request"))
        fenced, reason = fence(self._Row("82690", normalized_gate="implementation_done"))
        self.assertEqual(exempt, (False, ""))  # the review_request row is exempt
        self.assertTrue(fenced)  # the implementation_done row on the SAME journal is NOT
        self.assertIn("unresolvable", reason)

    def test_blank_gate_row_stays_fenced(self) -> None:
        # A row with no resolvable gate identity cannot be confirmed as the current review_request.
        fence = make_lane_gateway_send_edge_fence("", current_request_journal="82690")
        fenced, _ = fence(self._Row("82690", normalized_gate=""))
        self.assertTrue(fenced)

    def test_exemption_only_applies_under_unresolvable_anchor(self) -> None:
        # With a RESOLVABLE anchor the strict older-than-anchor fence stands; the current-request
        # exemption does NOT loosen it (a row older than a resolvable anchor is still superseded).
        fence = make_lane_gateway_send_edge_fence("82650", current_request_journal="82600")
        fenced, reason = fence(self._Row("82600"))
        self.assertTrue(fenced)
        self.assertIn("superseded", reason)

    def test_composed_supervisor_fence_exempts_current_row(self) -> None:
        source = _multi_generation_source()
        current_request = resolve_current_request_journal(source, ISSUE)
        self.assertEqual(current_request, "82690")
        fence = build_supervisor_send_edge_fence(
            None, "coordinator", HEAD_CURR, "", "", current_request
        )
        self.assertEqual(fence(self._Row("82690")), (False, ""))


def _multi_generation_source():
    """An issue with a completed previous generation + a current full-head correction request (#13948)."""
    return MappingRedmineJournalSource(
        payload={
            "issue": {"id": ISSUE},
            "journals": [
                {"id": "82600", "notes": f"[mozyo:workflow-event:gate=review_request:head={HEAD_PREV}:conclusion=pending]"},
                {"id": "82610", "notes": f"[mozyo:workflow-event:gate=review_result:conclusion=changes_requested:head={HEAD_PREV}:req=82600]"},
                {"id": "82690", "notes": f"[mozyo:workflow-event:gate=review_request:head={HEAD_CURR}:conclusion=pending]"},
            ],
        }
    )


class DiscoverResumedLaneTest(unittest.TestCase):
    def test_discovery_emits_only_current_gate(self) -> None:
        source = _multi_generation_source()
        candidates, _plans = discover_lane_gateway_sends(
            source, ISSUE, _owner(), workspace_id="wsA", dispatch_anchor_journal="",
        )
        self.assertEqual([c.journal for c in candidates], ["82690"])
        self.assertTrue(is_lane_gateway_route(candidates[0].callback_route))
        self.assertEqual(candidates[0].target_receiver, "codex")


class SupervisorResumedLaneScenarioTest(unittest.TestCase):
    """End-to-end: the fenced production supervisor with an unresolvable anchor (a resumed lane) still
    delivers the current full-head Review Request exactly once to the owning-lane gateway, coordinator
    direct wake 0, no duplicate — the #13948 j#82690 reproduction, now green."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.source = _multi_generation_source()
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def _run(self):
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
            owner_binding_fn=lambda wsid, issue, binding: _owner(),
            # A resumed lane: the dispatch-anchor resolver cannot pin a fresh IR marker -> None
            # (unresolvable). Before #14094 this fenced the current Review Request as previous-generation.
            candidate_fence_fn=lambda wsid, issue, source: None,
            clock=lambda: "2026-07-19T00:00:00+00:00",
        )
        report = sup.run_once()
        return sent, report

    def test_current_review_request_delivers_exactly_once(self) -> None:
        sent, report = self._run()
        self.assertEqual(len(sent), 1)
        self.assertTrue(is_lane_gateway_route(sent[0].callback_route))
        self.assertEqual(sent[0].journal, "82690")
        self.assertEqual(sent[0].target_lane, LANE)
        self.assertEqual(sent[0].target_receiver, "codex")
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].journal, "82690")
        # Coordinator direct wake 0: no coordinator-route row for the worker gate.
        self.assertEqual([r for r in self.outbox.read() if r.callback_route == "coordinator"], [])
        self.assertEqual(report.workspaces[0].delivered, 1)

    def test_second_sweep_does_not_duplicate(self) -> None:
        self._run()
        sent2, _ = self._run()
        # The outbox idempotency fence: the current gate already delivered, so a re-sweep re-sends nothing.
        self.assertEqual(sent2, [])
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 1)


if __name__ == "__main__":
    unittest.main()
