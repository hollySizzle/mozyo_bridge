"""Regression pin: review callback generation fence (Redmine #13974).

Fixed defect: the callback supervisor rebound a same-issue OLD review_result (a previous lane
generation's round) onto the CURRENT lane generation and enqueued a pending ``review_return`` row
that stayed retryable, growing the global backlog. The send-time refusals (``not_latest_review`` /
``generation_mismatch`` / ``review_round_stale``) fired but left the misbound row pending. Origin
commit ``20497da7`` (base ``a9dd5a7a``).

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

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
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
    RETURN_MISSING_REVIEW_HEAD,
    RETURN_PREVIOUS_GENERATION,
    RETURN_REVIEW_HEAD_DRIFT,
    is_review_return_route,
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
#: The exact commit heads (#13974 / j#81454 A): the current generation reviewed CUR_HEAD, the old one
#: OLD_HEAD. A git SHA is hex, safe inside the marker grammar.
CUR_HEAD = "cur00head"
OLD_HEAD = "old00head"


def _journals(*entries):
    return {"issue": {"id": ISSUE}, "journals": [{"id": jid, "notes": notes} for jid, notes in entries]}


def _req(head=None):
    return render_workflow_event_marker("review_request", target_head=head)


def _res(conclusion="approved", head=None, req=None):
    return render_workflow_event_marker(
        "review_result", conclusion=conclusion, target_head=head, review_request_journal=req
    )


def _old_round_source(*extra):
    """OLD generation round: review_request j10 -> review_result j20 (still the newest review marker)."""
    return MappingRedmineJournalSource(
        payload=_journals(
            ("10", _req(OLD_HEAD)), ("20", _res(head=OLD_HEAD, req="10")), *extra
        )
    )


def _current_round_source(*extra, conclusion="approved", head=CUR_HEAD):
    """CURRENT generation round: review_request j110 -> review_result j120 (both after the anchor)."""
    return MappingRedmineJournalSource(
        payload=_journals(
            ("110", _req(head)), ("120", _res(conclusion=conclusion, head=head, req="110")), *extra
        )
    )


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

    def _composed_fence(self, anchor, current_review_head=CUR_HEAD):
        """The exact send-edge fence the production supervisor composes for a fenced pass (#13974)."""
        return compose_send_edge_fences(
            make_send_edge_fence(anchor, "coordinator"),
            make_review_return_send_edge_fence(anchor, current_review_head),
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

    # -- full supervisor fan-out with the anchor + head fence wired ------

    def _register_ws(self, name="repoReview"):
        repo = self.home / name
        repo.mkdir()
        return register_workspace(repo, home=self.home).record.workspace_id

    def _build_supervisor(
        self, *, wsid, source, transport, anchor, holder="superF",
        inventory_lanes=(LANE,), roster_fn=None, authoritative_fn=None, workspaces_fn=None,
    ):
        """The REAL WorkspaceCallbackSupervisor with the #13968/#13974 anchor+head fence wired."""
        inv = lambda: (
            [{"name": encode_assigned_name(wsid, "codex", l), "pane_id": f"%{l}"} for l in inventory_lanes],
            "herdr",
        )

        def sender_fn(ws):
            resolver = BackendNeutralTargetResolver(
                workspace_id=ws.workspace_id, inventory=inv,
                live_generation_fn=owning_lane_generation_reader(ws.workspace_id, lifecycle_store=self.life),
            )
            return BackgroundServiceCallbackSender(
                workspace_id=ws.workspace_id, holder=holder, lease_store=self.lease,
                target_resolver=resolver, transport=transport, outbox=self.outbox, now_fn=lambda: NOW,
                round_fence_fn=review_round_send_fence(lambda: source),
            )

        return WorkspaceCallbackSupervisor(
            holder=holder, lease_store=self.lease, store=self.store, outbox=self.outbox,
            workspaces_fn=workspaces_fn
            or (lambda: [w for w in default_workspaces(home=self.home) if w.workspace_id == wsid]),
            roster_fn=roster_fn or (lambda ws: ((ISSUE,), "")),
            redmine_source_fn=lambda ws: source,
            sender_fn=sender_fn,
            binding_fn=lambda ws: RoleProviderBinding.default(),
            owner_binding_fn=lambda w, i, b: owning_lane_binding(w, i, b, lifecycle_store=self.life),
            release_after=False, clock=lambda: NOW,
            authoritative_fn=authoritative_fn,
            candidate_fence_fn=lambda w, i, s: anchor,
        )

    @staticmethod
    def _refusals(report):
        return [r for wso in report.workspaces for iso in wso.issues for r in iso.review_return_refusals]

    def _return_rows(self, states):
        """Only the ``review_return:<lane>`` rows in the given states (the review markers on the source
        also spawn separate coordinator-route callbacks; those are out of scope for these assertions)."""
        return [r for r in self.outbox.read(states=states) if is_review_return_route(r.callback_route)]

    def test_full_supervisor_fences_the_previous_generation_return(self) -> None:
        wsid = self._register_ws()
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_old_round_source(), transport=transport, anchor=ANCHOR,
        )
        report = supervisor.run_once()
        # enqueue 0, send 0, nothing pending — the previous-generation return never lands.
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        self.assertIn(RETURN_PREVIOUS_GENERATION, self._refusals(report))

    # -- F2 required adversarial matrix (through the same production composition) ---

    def test_matrix_current_changes_requested_delivers_exactly_once(self) -> None:
        # Required matrix: a CURRENT-generation changes_requested (not just approval) is a valid review
        # outcome and must deliver exactly once through the full fenced supervisor.
        wsid = self._register_ws()
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        transport = _CapturingTransport()
        source = _current_round_source(conclusion="changes_requested")
        supervisor = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        # enqueue 1 -> send 1 -> delivered 1 review_return row to the owning lane, none left pending.
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        # A second pass re-discovers the same review_result: idempotent, no second send.
        supervisor.run_once()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)

    def test_matrix_missing_target_head_is_terminal_zero_send(self) -> None:
        # Required matrix: a current-generation round whose review markers carry NO head (a legacy /
        # head-less producer) is head-unconfirmable -> refused at discovery (0-enqueue), never sent.
        wsid = self._register_ws()
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        transport = _CapturingTransport()
        headless = _current_round_source(head=None)  # req/result markers carry no head
        supervisor = self._build_supervisor(wsid=wsid, source=headless, transport=transport, anchor=ANCHOR)
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_MISSING_REVIEW_HEAD, self._refusals(report))

    def test_matrix_head_drift_row_is_refused(self) -> None:
        # Required matrix (head witness): the result reviewed a DIFFERENT head than its request pinned
        # -> drift -> refused at discovery, never returned to the current lane.
        self._declare_owner()
        drift = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head="otherhead", req="110")))
        )
        candidates, plans = discover_review_returns(
            drift, ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any(p.reason == RETURN_REVIEW_HEAD_DRIFT for p in plans))

    def test_matrix_preexisting_current_row_with_drifted_head_is_terminal(self) -> None:
        # A row enqueued for a head that has since been superseded (the current review generation moved
        # to a new head) must reach a terminal disposition at the send edge, not retry.
        self._declare_owner()
        # Enqueue a current-generation row recording OLD_HEAD via unfenced discovery of a head-bearing
        # round whose request is >= anchor.
        stale_head_source = MappingRedmineJournalSource(
            payload=_journals(("110", _req(OLD_HEAD)), ("120", _res(head=OLD_HEAD, req="110")))
        )
        candidates, _ = discover_review_returns(
            stale_head_source, ISSUE, self._owner(), workspace_id=WS, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(len(candidates), 1)  # emitted while OLD_HEAD was current
        proc = CallbackOutboxProcessor(self.outbox, stale_head_source, workspace_id=WS)
        proc.ingest(candidates, now=NOW)
        # The current review generation head has since advanced to CUR_HEAD; the pending row drifted.
        transport = _CapturingTransport()
        run_once(
            proc, self._sender(transport, fence_source=stale_head_source), now=NOW,
            send_fence_fn=self._composed_fence(ANCHOR, current_review_head=CUR_HEAD),
        )
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        self.assertTrue(self.outbox.read(states=[CALLBACK_UNCERTAIN]))

    def test_matrix_hibernate_resume_previous_generation_fenced(self) -> None:
        # Required matrix: after a lane hibernates and resumes, the dispatch anchor advances to the
        # resumed generation's IR. A review from BEFORE the resume (round < the resumed anchor) is a
        # previous generation and is fenced — never retargeted onto the resumed lane.
        wsid = self._register_ws()
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        transport = _CapturingTransport()
        # The round is j110/j120 (head CUR_HEAD); the resumed generation's anchor is j200 (> j120).
        resumed_anchor = "200"
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=resumed_anchor,
        )
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())
        self.assertIn(RETURN_PREVIOUS_GENERATION, self._refusals(report))

    def test_matrix_duplicate_supervisor_lease_fence_no_double_delivery(self) -> None:
        # Required matrix: a second supervisor that loses the workspace lease delivers nothing — the
        # current-generation return is delivered exactly once, never doubled by a duplicate supervisor.
        wsid = self._register_ws()
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        transport = _CapturingTransport()
        source = _current_round_source()
        first = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR, holder="superF")
        first.run_once()
        self.assertEqual(len(transport.calls), 1)  # delivered once by the lease holder
        # A duplicate supervisor (different holder) races the SAME workspace; the lease fence refuses it.
        second = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR, holder="superDUP")
        second.run_once()
        self.assertEqual(len(transport.calls), 1)  # no second delivery
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_DELIVERED])), 1)

    def test_matrix_cross_workspace_duplicate_journal_dropped(self) -> None:
        # Required matrix: the SAME issue journal is visible to a FOREIGN workspace's roster, but the
        # authoritative-workspace map pins it to its owning workspace. The foreign supervisor drops it
        # (0-enqueue / 0-send) — the generation fence never even runs on a non-authoritative issue.
        owner_wsid = self._register_ws("repoOwner")
        foreign_wsid = self._register_ws("repoForeign")
        self.life.declare_active(
            LaneLifecycleKey(owner_wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, "100"), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(foreign_wsid, "superF", now=NOW, ttl_seconds=600)
        transport = _CapturingTransport()
        # The foreign supervisor sees ISSUE in its roster but the authoritative map pins it elsewhere.
        supervisor = self._build_supervisor(
            wsid=foreign_wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
            workspaces_fn=lambda: [w for w in default_workspaces(home=self.home) if w.workspace_id == foreign_wsid],
            authoritative_fn=lambda: {ISSUE: owner_wsid},
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_DELIVERED]), ())
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())


if __name__ == "__main__":
    unittest.main()
