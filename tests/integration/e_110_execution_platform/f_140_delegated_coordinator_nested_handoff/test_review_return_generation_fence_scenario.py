"""Review callback generation fence — production-composition scenario (Redmine #13974).

The regression evidence for the #13974 bug: the callback supervisor rebound a same-issue OLD
review_result (a previous lane generation's round) onto the CURRENT lane generation and enqueued a
pending ``review_return`` row that stayed retryable, growing the global backlog. The send-time
refusals (``not_latest_review`` / ``generation_mismatch`` / ``review_round_stale``) fired but left
the misbound row pending.

This exercises the two-sided fix through the REAL machinery (real outbox, real lifecycle owning-lane
binding, real background_service authority; only the Redmine source + transport faked):

- discovery-edge fence: a previous-generation review round is refused at discovery (0-enqueue),
  fail-closed on an unresolvable anchor, and the current generation's round still emits;
- send-edge fence: a PRE-EXISTING misbound pending row reaches a terminal (uncertain) disposition,
  never remaining retryable — and a current-generation row is exempt so exactly-once is preserved;
- the full ``WorkspaceCallbackSupervisor`` fan-out with the anchor fence wired end-to-end;
- adversarial witnesses that the fence is load-bearing (without it the misbound row enqueues /
  survives) and that restart / duplicate passes never resurrect a terminally-fenced row.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    RETURN_PREVIOUS_GENERATION,
    make_review_return_send_edge_fence,
    review_return_callback_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    compose_send_edge_fences,
    make_send_edge_fence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

NOW = "2026-07-13T00:00:00+00:00"
WS = "wsReview"
ISSUE = "13974"
LANE = "issue_13974"
#: The current generation's dispatch anchor: the OLD round (j10/j20) precedes it; a current round
#: (j110/j120) follows it. Redmine journal ids are monotonic, so a numeric compare is chronological.
ANCHOR = "100"


def _journals(*entries):
    return {"issue": {"id": ISSUE}, "journals": [{"id": jid, "notes": notes} for jid, notes in entries]}


def _req():
    return render_workflow_event_marker("review_request")


def _res(conclusion="approved"):
    return render_workflow_event_marker("review_result", conclusion=conclusion)


def _old_round_source(*extra):
    """OLD generation round: review_request j10 -> review_result j20 (still the newest review marker)."""
    return MappingRedmineJournalSource(payload=_journals(("10", _req()), ("20", _res()), *extra))


def _current_round_source(*extra):
    """CURRENT generation round: review_request j110 -> review_result j120 (both after the anchor)."""
    return MappingRedmineJournalSource(payload=_journals(("110", _req()), ("120", _res()), *extra))


class _CapturingTransport:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or HandoffDeliveryResult("sent", "ok")

    def deliver(self, row, target):
        self.calls.append((row, target))
        return self._result


class ReviewReturnGenerationFenceScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store_path = workflow_runtime_store_path(self.home)
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.life = LaneLifecycleStore(home=self.home)
        self.lease = SupervisorLeaseStore(path=supervisor_lease_path(self.home))
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

    def _owner(self):
        return owning_lane_binding(WS, ISSUE, RoleProviderBinding.default(), lifecycle_store=self.life)

    def _inventory(self, *lanes):
        rows = [{"name": encode_assigned_name(WS, "codex", lane), "pane_id": f"%{lane}"} for lane in lanes]
        return lambda: (rows, "herdr")

    def _sender(self, transport, *, inventory_lanes=(LANE,), fence_source=None):
        resolver = BackendNeutralTargetResolver(
            workspace_id=WS,
            inventory=self._inventory(*inventory_lanes),
            live_generation_fn=owning_lane_generation_reader(WS, lifecycle_store=self.life),
        )
        fence = review_round_send_fence(lambda: fence_source) if fence_source is not None else None
        return BackgroundServiceCallbackSender(
            workspace_id=WS, holder="superX", lease_store=self.lease,
            target_resolver=resolver, transport=transport, outbox=self.outbox,
            now_fn=lambda: NOW, round_fence_fn=fence,
        )

    def _composed_fence(self, anchor):
        """The exact send-edge fence the production supervisor composes for a fenced pass."""
        return compose_send_edge_fences(
            make_send_edge_fence(anchor, "coordinator"),
            make_review_return_send_edge_fence(anchor),
        )

    # -- discovery-edge fence --------------------------------------------

    def test_previous_generation_review_is_refused_at_discovery(self) -> None:
        # The core repro: the OLD round's result (j20) is still the newest review marker on the issue,
        # and the current owner (LANE) resolves — but the round (j10) predates the current dispatch
        # anchor (j100), so with the anchor threaded discovery refuses it (0-enqueue).
        self._declare_owner()
        source = _old_round_source()
        candidates, plans = discover_review_returns(
            source, ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any(p.reason == RETURN_PREVIOUS_GENERATION for p in plans))

    def test_without_the_anchor_the_old_round_would_be_misbound(self) -> None:
        # Adversarial witness that the fence is load-bearing: the UNFENCED discovery (anchor=None, the
        # pre-#13974 behavior) DOES emit the old round bound to the current lane — the exact bug.
        self._declare_owner()
        candidates, _ = discover_review_returns(_old_round_source(), ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].callback_route, review_return_callback_route(LANE))

    def test_unresolvable_anchor_under_fence_is_fail_closed(self) -> None:
        # A fenced pass that cannot pin the current generation ("") refuses every return (fail-closed).
        self._declare_owner()
        candidates, plans = discover_review_returns(
            _old_round_source(), ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=""
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any(p.reason == RETURN_PREVIOUS_GENERATION for p in plans))

    def test_current_generation_round_still_delivers_exactly_once(self) -> None:
        # Exactly-once for the CURRENT generation (requirement 4): a round produced under the current
        # generation (request j110 >= anchor j100) emits and delivers once, unaffected by the fence.
        self._declare_owner()
        source = _current_round_source()
        candidates, _ = discover_review_returns(
            source, ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(len(candidates), 1)
        transport = _CapturingTransport()
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        run_once(
            proc, self._sender(transport, fence_source=source), candidates=candidates, now=NOW,
            send_fence_fn=self._composed_fence(ANCHOR),
        )
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 1)

    # -- send-edge terminal fence (pre-existing misbound rows) -----------

    def _ingest_misbound_row(self):
        """Enqueue the misbound OLD-round return row exactly as the pre-#13974 supervisor would have."""
        source = _old_round_source()
        candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        self.assertEqual(len(candidates), 1)  # unfenced discovery produced the misbound row
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        proc.ingest(candidates, now=NOW)
        self.assertTrue(self.outbox.read(states=[CALLBACK_PENDING]))
        return source

    def test_preexisting_misbound_row_reaches_terminal_not_retryable(self) -> None:
        self._declare_owner()
        source = self._ingest_misbound_row()
        transport = _CapturingTransport()
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        run_once(
            proc, self._sender(transport, fence_source=source), now=NOW,
            send_fence_fn=self._composed_fence(ANCHOR),
        )
        # Zero-send, terminal (uncertain), and NOT left pending/retryable.
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        self.assertTrue(self.outbox.read(states=[CALLBACK_UNCERTAIN]))

    def test_without_the_send_fence_the_misbound_row_stays_pending(self) -> None:
        # Adversarial witness: WITHOUT the composed fence, the misbound row is NOT terminal — the
        # send-time refusal path leaves it pending/retryable (the backlog-growth symptom).
        self._declare_owner()
        source = self._ingest_misbound_row()
        transport = _CapturingTransport()
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        # No send_fence_fn; but a NEWER request lands so the sender's round fence still zero-sends.
        run_once(proc, self._sender(transport, fence_source=_old_round_source(("30", _req()))), now=NOW)
        self.assertEqual(transport.calls, [])
        # The row is NOT terminal — it remains pending (retryable), proving the send fence is needed.
        self.assertTrue(self.outbox.read(states=[CALLBACK_PENDING]))
        self.assertEqual(self.outbox.read(states=[CALLBACK_UNCERTAIN]), ())

    def test_terminally_fenced_row_is_not_resurrected_on_restart(self) -> None:
        # Restart / backlog-replay: after the row is terminally fenced, a second fenced pass never
        # resurrects it to pending nor re-delivers it.
        self._declare_owner()
        source = self._ingest_misbound_row()
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        run_once(proc, self._sender(_CapturingTransport(), fence_source=source), now=NOW,
                 send_fence_fn=self._composed_fence(ANCHOR))
        self.assertTrue(self.outbox.read(states=[CALLBACK_UNCERTAIN]))
        # Second pass (restart): re-discover (fenced -> 0 new) + re-deliver.
        candidates, _ = discover_review_returns(
            source, ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(candidates, [])
        transport2 = _CapturingTransport()
        proc2 = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        run_once(proc2, self._sender(transport2, fence_source=source), candidates=candidates, now=NOW,
                 send_fence_fn=self._composed_fence(ANCHOR))
        self.assertEqual(transport2.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())

    # -- full supervisor fan-out with the anchor fence wired -------------

    def test_full_supervisor_fences_the_previous_generation_return(self) -> None:
        repo = self.home / "repoReview"
        repo.mkdir()
        rec = register_workspace(repo, home=self.home).record
        wsid = rec.workspace_id
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        source = _old_round_source()
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
                round_fence_fn=review_round_send_fence(lambda: source),
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
            # The #13968/#13974 production anchor: this issue's current generation opened at j100.
            candidate_fence_fn=lambda w, i, s: ANCHOR,
        )
        report = supervisor.run_once()
        # The previous-generation return is never delivered, and nothing is left pending.
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        # The refusal reason is surfaced in the redaction-safe report (observability, not a silent drop).
        refusals = [
            r
            for wso in report.workspaces
            for iso in wso.issues
            for r in iso.review_return_refusals
        ]
        self.assertIn(RETURN_PREVIOUS_GENERATION, refusals)


if __name__ == "__main__":
    unittest.main()
