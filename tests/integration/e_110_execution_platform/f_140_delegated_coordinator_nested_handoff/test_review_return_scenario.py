"""Correlated review_result return — production-composition scenario (Redmine #13684).

The full stateful path through the REAL machinery — real callback outbox, real supervisor lease,
real backend-neutral resolver, real background_service authority, and the REAL #13681/#13689 lane
lifecycle owning-lane binding — with only the Redmine source, the delivery transport, and the live
Herdr inventory faked (the Phase B dogfood surfaces). This is the evidence the delivery seam
#13683 left fail-closed-disabled is now correctly ENABLED for the correlated review_result return,
and fails closed for every stale / superseded / ambiguous / self-route case (design answer j#77892
required acceptance):

- a coordinator-recorded review_result returns to its owning-lane Codex gateway exactly once;
- a supersession zero-sends the stale-lane row and follows the durable owning-lane binding to the
  recovery lane (never a "current-looking" pane);
- the latest-review fence refuses a stale result (a newer review_request restarted the round);
- a duplicate review_result journal enqueues nothing new and delivers once;
- a self-route (coordinator-owned issue), a missing owner, and an uncertain send all fail closed;
- the coordinator callback route stays generation-disabled (unchanged Phase A residual).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer, LaneLifecycleKey
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore, supervisor_lease_path
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
    WorkflowRuntimeStore,
    workflow_runtime_store_path,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackendNeutralTargetResolver,
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    discover_review_returns,
    run_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    default_workspaces,
    owning_lane_binding,
    owning_lane_generation_reader,
    review_round_send_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    render_workflow_event_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

NOW = "2026-07-13T00:00:00+00:00"
WS = "wsReview"
ISSUE = "13684"
LANE = "issue_13684"
RECOVERY_LANE = "recovery_13684"


def _journals(*entries):
    return {"issue": {"id": ISSUE}, "journals": [{"id": jid, "notes": notes} for jid, notes in entries]}


def _review_request_note():
    return render_workflow_event_marker("review_request")


def _review_result_note(conclusion="approved"):
    return render_workflow_event_marker("review_result", conclusion=conclusion)


def _round_source(*extra):
    """A source with a valid review round (review_request j10 → review_result j20) + optional extras."""
    entries = [("10", _review_request_note()), ("20", _review_result_note()), *extra]
    return MappingRedmineJournalSource(payload=_journals(*entries))


class _CapturingTransport:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or HandoffDeliveryResult("sent", "ok")

    def deliver(self, row, target):
        self.calls.append((row, target))
        return self._result


class ReviewReturnScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store_path = workflow_runtime_store_path(self.home)
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.life = LaneLifecycleStore(home=self.home)
        self.lease = SupervisorLeaseStore(path=supervisor_lease_path(self.home))
        # This authority holds the workspace supervisor lease for the whole scenario.
        self.lease.acquire(WS, "superX", now=NOW, ttl_seconds=600)

    # -- helpers ----------------------------------------------------------

    def _declare_owner(self, lane=LANE, journal="100"):
        out = self.life.declare_active(
            LaneLifecycleKey(WS, lane),
            decision=DecisionPointer("redmine", ISSUE, journal),
            issue_id=ISSUE,
            now=NOW,
        )
        self.assertTrue(out.applied, out)

    def _supersede(self, superseded=LANE, recovery=RECOVERY_LANE, journal="200"):
        out = self.life.supersede_and_activate(
            superseded=LaneLifecycleKey(WS, superseded),
            expected_revision=1,
            recovery=LaneLifecycleKey(WS, recovery),
            decision=DecisionPointer("redmine", ISSUE, journal),
            now=NOW,
        )
        self.assertTrue(out.applied, out)

    def _inventory(self, *lanes):
        rows = [{"name": encode_assigned_name(WS, "codex", lane), "pane_id": f"%{lane}"} for lane in lanes]
        return lambda: (rows, "herdr")

    def _sender(self, transport, *, inventory_lanes=(LANE,), fence_source=None):
        resolver = BackendNeutralTargetResolver(
            workspace_id=WS,
            inventory=self._inventory(*inventory_lanes),
            live_generation_fn=owning_lane_generation_reader(WS, lifecycle_store=self.life),
        )
        # Wire the REAL action-time round fence (R1-F1) against a live source so the send edge
        # re-verifies the review round; ``fence_source`` may differ from the discovery source to
        # simulate a newer review_request landing between reserve and send.
        fence = review_round_send_fence(lambda: fence_source) if fence_source is not None else None
        return BackgroundServiceCallbackSender(
            workspace_id=WS, holder="superX", lease_store=self.lease,
            target_resolver=resolver, transport=transport, outbox=self.outbox,
            now_fn=lambda: NOW, round_fence_fn=fence,
        )

    def _owner(self):
        return owning_lane_binding(WS, ISSUE, RoleProviderBinding.default(), lifecycle_store=self.life)

    def _run(self, source, transport, *, inventory_lanes=(LANE,), candidates=None, fence_source=None):
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        if candidates is None:
            candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        sender = self._sender(
            transport, inventory_lanes=inventory_lanes,
            fence_source=fence_source if fence_source is not None else source,
        )
        return run_once(proc, sender, candidates=candidates, now=NOW)

    # -- scenarios --------------------------------------------------------

    def test_correlated_review_result_returns_to_owning_gateway_once(self) -> None:
        self._declare_owner()
        source = MappingRedmineJournalSource(
            payload=_journals(("10", _review_request_note()), ("20", _review_result_note()))
        )
        candidates, plans = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.callback_route, "review_return:issue_13684")
        self.assertEqual((c.target_lane, c.target_receiver, c.target_generation), (LANE, "codex", "1"))

        transport = _CapturingTransport()
        self._run(source, transport, candidates=candidates)
        # Delivered exactly once, to the owning lane's live gateway locator.
        self.assertEqual(len(transport.calls), 1)
        _row, target = transport.calls[0]
        self.assertEqual((target.receiver, target.lane, target.locator), ("codex", LANE, "%issue_13684"))
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual([d.callback_route for d in delivered], ["review_return:issue_13684"])

    def test_duplicate_review_result_journal_delivers_once(self) -> None:
        self._declare_owner()
        source = _round_source()
        t1 = _CapturingTransport()
        self._run(source, t1)
        # A second sweep re-discovers the same review_result — idempotent enqueue, nothing new to send.
        t2 = _CapturingTransport()
        self._run(source, t2)
        self.assertEqual(len(t1.calls), 1)
        self.assertEqual(t2.calls, [])
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 1)

    def test_latest_review_fence_refuses_a_stale_result_at_discovery(self) -> None:
        self._declare_owner()
        # Round j10→j20, then a newer review_request (j30) restarted the round after the result.
        source = _round_source(("30", _review_request_note()))
        candidates, plans = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(candidates, [])
        transport = _CapturingTransport()
        self._run(source, transport, candidates=candidates)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())

    def test_action_time_fence_zero_sends_when_a_newer_request_lands_after_reserve(self) -> None:
        # R1-F1 race regression: the return row is reserved while the round is current, then a NEWER
        # review_request lands before the send edge. The action-time round fence (re-reading the live
        # markers at send time) must zero-send the now-stale row — discovery-time correctness alone is
        # not enough.
        self._declare_owner()
        discovery_source = _round_source()  # j10 req, j20 result — current at reserve
        candidates, _ = discover_review_returns(discovery_source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(len(candidates), 1)
        proc = CallbackOutboxProcessor(self.outbox, discovery_source, workspace_id=WS)
        proc.ingest(candidates, now=NOW)  # reserve the return row while the round is current
        # A newer review_request (j30) restarts the round AFTER reserve.
        send_source = _round_source(("30", _review_request_note()))
        transport = _CapturingTransport()
        run_once(proc, self._sender(transport, fence_source=send_source), now=NOW)
        self.assertEqual(transport.calls, [])  # action-time fence zero-sends the stale round
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        # Without the action-time fence the same row WOULD have delivered — prove the fence is load-bearing.
        self.setUp()  # fresh stores
        self._declare_owner()
        candidates2, _ = discover_review_returns(_round_source(), ISSUE, self._owner(), workspace_id=WS)
        proc2 = CallbackOutboxProcessor(self.outbox, _round_source(), workspace_id=WS)
        proc2.ingest(candidates2, now=NOW)
        t2 = _CapturingTransport()
        # fence_source omitted -> no round fence; the stale round is not caught (regression witness).
        run_once(proc2, self._sender(t2), now=NOW)
        self.assertEqual(len(t2.calls), 1)

    def test_supersession_zero_sends_the_stale_lane_row(self) -> None:
        # An L1 return row was reserved while L1 owned the issue.
        self._declare_owner(lane=LANE)
        source = _round_source()
        candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        proc.ingest(candidates, now=NOW)  # enqueue the L1 return row, do not deliver yet
        # Now a supersession hands ownership to the recovery lane — the L1 row is stale.
        self._supersede()
        transport = _CapturingTransport()
        # The stale L1 pane may even still be live; the generation fence (independent live authority)
        # is what zero-sends it, not the pane's absence.
        run_once(proc, self._sender(transport, inventory_lanes=(LANE, RECOVERY_LANE)), now=NOW)
        self.assertEqual(transport.calls, [])  # zero-send
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        # The row is retryable (pending), never a blind uncertain.
        self.assertTrue(self.outbox.read(states=[CALLBACK_PENDING]))

    def test_supersession_follows_binding_to_recovery_lane(self) -> None:
        # The owner is the recovery lane by the time the review_result is discovered.
        self._declare_owner(lane=LANE)
        self._supersede()  # owner is now RECOVERY_LANE
        source = _round_source()
        candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].callback_route, f"review_return:{RECOVERY_LANE}")
        self.assertEqual(candidates[0].target_lane, RECOVERY_LANE)
        transport = _CapturingTransport()
        self._run(source, transport, inventory_lanes=(RECOVERY_LANE,), candidates=candidates)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1].lane, RECOVERY_LANE)

    def test_self_route_owned_by_coordinator_lane_emits_nothing(self) -> None:
        self._declare_owner(lane="default")  # the coordinator's own lane owns the issue
        source = _round_source()
        candidates, plans = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(candidates, [])
        self.assertTrue(any(p.reason == "self_route" for p in plans))

    def test_missing_owner_emits_nothing(self) -> None:
        # No lifecycle owner row for the issue -> fail-closed, no return.
        source = _round_source()
        candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(candidates, [])

    def test_gateway_absent_from_live_inventory_zero_sends(self) -> None:
        # The owning lane resolves, but its Codex gateway pane is not in the live inventory (down /
        # not launched) -> no live target -> deterministic zero-send, row stays retryable.
        self._declare_owner()
        source = _round_source()
        transport = _CapturingTransport()
        # inventory_lanes empty -> the resolver finds no live gateway for LANE.
        self._run(source, transport, inventory_lanes=())
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        self.assertTrue(self.outbox.read(states=[CALLBACK_PENDING]))

    def test_uncertain_send_is_not_blind_retried(self) -> None:
        self._declare_owner()
        source = _round_source()
        # A post-injection ambiguous outcome (turn-start unconfirmed) -> uncertain, never auto-retried.
        transport = _CapturingTransport(result=HandoffDeliveryResult("blocked", "turn_start_unconfirmed"))
        self._run(source, transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(self.outbox.read(states=[CALLBACK_UNCERTAIN]))
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())

    def test_full_supervisor_fan_out_delivers_the_return(self) -> None:
        # The strongest production-composition proof: the REAL WorkspaceCallbackSupervisor fan-out
        # with owner_binding_fn wired delivers the correlated return through the whole path.
        repo = self.home / "repoReview"
        repo.mkdir()
        rec = register_workspace(repo, home=self.home).record
        wsid = rec.workspace_id
        # Own the issue in the registered workspace; declare + fake inventory keyed on THAT id.
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        source = _round_source()
        transport = _CapturingTransport()
        inv = lambda: ([{"name": encode_assigned_name(wsid, "codex", LANE), "pane_id": "%gw"}], "herdr")

        def sender_fn(ws):
            resolver = BackendNeutralTargetResolver(
                workspace_id=ws.workspace_id, inventory=inv,
                live_generation_fn=owning_lane_generation_reader(ws.workspace_id, lifecycle_store=self.life),
            )
            return BackgroundServiceCallbackSender(
                workspace_id=ws.workspace_id, holder="superF", lease_store=self.lease,
                target_resolver=resolver, transport=transport, outbox=self.outbox, now_fn=lambda: NOW,
            )

        supervisor = WorkspaceCallbackSupervisor(
            holder="superF", lease_store=self.lease, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [w for w in default_workspaces(home=self.home) if w.workspace_id == wsid],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=lambda ws: source,
            sender_fn=sender_fn,
            binding_fn=lambda ws: RoleProviderBinding.default(),
            owner_binding_fn=lambda w, i, b: owning_lane_binding(w, i, b, lifecycle_store=self.life),
            release_after=False,
            clock=lambda: NOW,
        )
        supervisor.run_once()
        # The correlated return was delivered to the owning gateway through the full fan-out.
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertIn("review_return:issue_13684", {d.callback_route for d in delivered})


if __name__ == "__main__":
    unittest.main()
