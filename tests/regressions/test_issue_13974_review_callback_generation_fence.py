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
from unittest import mock

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
    RETURN_AMBIGUOUS_REVIEW_IDENTITY,
    RETURN_MALFORMED_REVIEW_HEAD,
    RETURN_MISSING_REVIEW_CONCLUSION,
    RETURN_MISSING_REVIEW_HEAD,
    RETURN_PREVIOUS_GENERATION,
    RETURN_REVIEW_HEAD_DRIFT,
    RETURN_REVIEW_REQUEST_UNCONFIRMED,
    is_review_return_route,
    review_return_callback_route,
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
#: The exact FULL commit heads (#13974 / j#81454 A + j#81487 F1): the current generation reviewed
#: CUR_HEAD, the old one OLD_HEAD. Full heads are 40 (or 64) lowercase hex; the v2 fence rejects any
#: non-full head, so fixtures must use well-formed SHAs.
CUR_HEAD = "c" * 40
OLD_HEAD = "d" * 40


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

    def test_send_time_round_fence_terminalizes_deterministic_stale_row(self) -> None:
        # R8-F1: even with ONLY the action-time round fence wired (no composed send-edge fence), a
        # misbound row that a READABLE provider deterministically supersedes is TERMINAL at the send
        # edge — zero-send, marked uncertain (retry 0, operator-visible), NOT a bounded-retry pending
        # row. This is the #13974 backlog-retention failure closed at the final authority: previously
        # this same path left the row pending (attempts=1). The fenced supervisor matrix runs below.
        self._declare_owner()
        source = _old_round_source()
        candidates, _ = discover_review_returns(source, ISSUE, self._owner(), workspace_id=WS)
        proc = CallbackOutboxProcessor(self.outbox, source, workspace_id=WS)
        proc.ingest(candidates, now=NOW)
        transport = _CapturingTransport()
        # No send_fence_fn; a NEWER request (j30) lands so the round fence sees a deterministic stale.
        run_once(proc, self._sender(transport, fence_source=_old_round_source(("30", _req()))), now=NOW)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.outbox.read(states=[CALLBACK_PENDING]), ())  # no longer retryable-pending
        terminal = self.outbox.read(states=[CALLBACK_UNCERTAIN])
        self.assertTrue(terminal)  # terminal zero-send
        self.assertEqual([r.attempts for r in terminal], [0])  # retry 0 — never bounded-retried

    # -- full supervisor fan-out with the anchor + head fence wired ------

    def _register_ws(self, name="repoReview"):
        repo = self.home / name
        repo.mkdir()
        return register_workspace(repo, home=self.home).record.workspace_id

    def _build_supervisor(
        self, *, wsid, source, transport, anchor, holder="superF",
        inventory_lanes=(LANE,), roster_fn=None, authoritative_fn=None, workspaces_fn=None,
        round_fence_source=None,
    ):
        """The REAL WorkspaceCallbackSupervisor with the #13968/#13974 anchor+head fence wired.

        ``round_fence_source`` (j#81518 F2) lets the SENDER's final live round fence read a DIFFERENT
        source than the supervisor's snapshot — the two-read TOCTOU. Defaults to ``source``."""
        inv = lambda: (
            [{"name": encode_assigned_name(wsid, "codex", l), "pane_id": f"%{l}"} for l in inventory_lanes],
            "herdr",
        )
        fence_src = round_fence_source if round_fence_source is not None else source

        def sender_fn(ws):
            resolver = BackendNeutralTargetResolver(
                workspace_id=ws.workspace_id, inventory=inv,
                live_generation_fn=owning_lane_generation_reader(ws.workspace_id, lifecycle_store=self.life),
            )
            return BackgroundServiceCallbackSender(
                workspace_id=ws.workspace_id, holder=holder, lease_store=self.lease,
                target_resolver=resolver, transport=transport, outbox=self.outbox, now_fn=lambda: NOW,
                round_fence_fn=review_round_send_fence(lambda: fence_src),
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

    def _own_and_lease(self, wsid, journal="100"):
        """Declare the issue owner under ``wsid`` and hold the workspace lease (supervisor prereqs)."""
        self.life.declare_active(
            LaneLifecycleKey(wsid, LANE),
            decision=DecisionPointer("redmine", ISSUE, journal), issue_id=ISSUE, now=NOW,
        )
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)

    def _enqueue_via_unfenced_discovery(self, source, wsid):
        """Enqueue a review_return row exactly as a pre-#13974 (unfenced) supervisor pass would have,
        so the terminal cases exercise a row that ALREADY exists when the fenced supervisor runs."""
        owner = owning_lane_binding(wsid, ISSUE, RoleProviderBinding.default(), lifecycle_store=self.life)
        candidates, _ = discover_review_returns(source, ISSUE, owner, workspace_id=wsid)
        CallbackOutboxProcessor(self.outbox, source, workspace_id=wsid).ingest(candidates, now=NOW)
        return candidates

    def _enqueue_via_fenced_discovery(self, source, wsid):
        """Enqueue a row via FENCED discovery so the payload carries the full v2 identity
        (req + head + conclusion) — the load-bearing witness for the send-edge conclusion/head fences
        (j#81512 F2): without this the payload lacks head/conclusion and the head fence fires first."""
        owner = owning_lane_binding(wsid, ISSUE, RoleProviderBinding.default(), lifecycle_store=self.life)
        candidates, _ = discover_review_returns(
            source, ISSUE, owner, workspace_id=wsid, dispatch_anchor_journal=ANCHOR
        )
        self.assertEqual(len(candidates), 1, "fenced discovery should emit a full-identity row")
        CallbackOutboxProcessor(self.outbox, source, workspace_id=wsid).ingest(candidates, now=NOW)
        return candidates

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

    def test_matrix_current_approval_delivers_exactly_once(self) -> None:
        # Required matrix: a CURRENT-generation approval delivers exactly once through the full fenced
        # supervisor; a second pass is idempotent (no second send).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        supervisor.run_once()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)

    def test_matrix_current_changes_requested_delivers_exactly_once(self) -> None:
        # Required matrix: a CURRENT-generation changes_requested (not just approval) is a valid review
        # outcome and must deliver exactly once through the full fenced supervisor.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        source = _current_round_source(conclusion="changes_requested")
        supervisor = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        supervisor.run_once()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)

    def test_matrix_missing_target_head_is_terminal_zero_send(self) -> None:
        # Required matrix: a current-generation round whose review markers carry NO head (a legacy /
        # head-less producer) is head-unconfirmable -> refused at discovery (0-enqueue), never sent.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        headless = _current_round_source(head=None)  # req/result markers carry no head
        supervisor = self._build_supervisor(wsid=wsid, source=headless, transport=transport, anchor=ANCHOR)
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_MISSING_REVIEW_HEAD, self._refusals(report))

    def test_matrix_missing_declared_req_is_terminal_zero_send(self) -> None:
        # Required matrix (F1 witness): a review_result marker that declares no `req` is refused at
        # discovery through the supervisor (0-enqueue / 0-send).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        no_req = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head=CUR_HEAD, req=None)))
        )
        supervisor = self._build_supervisor(wsid=wsid, source=no_req, transport=transport, anchor=ANCHOR)
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_REVIEW_REQUEST_UNCONFIRMED, self._refusals(report))

    def test_matrix_malformed_head_is_terminal_zero_send(self) -> None:
        # Required matrix (F1 witness): a matching but NON-full-hex head is malformed -> refused at
        # discovery through the supervisor.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        bad = MappingRedmineJournalSource(
            payload=_journals(("110", _req("deadbeef")), ("120", _res(head="deadbeef", req="110")))
        )
        supervisor = self._build_supervisor(wsid=wsid, source=bad, transport=transport, anchor=ANCHOR)
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_MALFORMED_REVIEW_HEAD, self._refusals(report))

    def test_matrix_head_drift_is_refused(self) -> None:
        # Required matrix (head witness): the result reviewed a DIFFERENT head than its request pinned
        # -> drift -> refused at discovery through the supervisor, never returned to the current lane.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        drift = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head=OLD_HEAD, req="110")))
        )
        supervisor = self._build_supervisor(wsid=wsid, source=drift, transport=transport, anchor=ANCHOR)
        report = supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_REVIEW_HEAD_DRIFT, self._refusals(report))

    def test_matrix_preexisting_misbound_row_terminal_and_restart(self) -> None:
        # Required matrix: a PRE-EXISTING misbound (old-round) pending row reaches a terminal
        # disposition through the fenced supervisor and is not resurrected on restart / backlog replay.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_unfenced_discovery(_old_round_source(), wsid)
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))  # a real pre-existing row
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_old_round_source(), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))
        # Restart / backlog replay: a second supervisor pass never resurrects it to pending / delivered.
        supervisor.run_once()
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])

    def test_matrix_preexisting_drifted_head_row_terminal_via_supervisor(self) -> None:
        # Required matrix: a current-generation row enqueued for OLD_HEAD, then the review generation
        # head advances (a newer review_request pins CUR_HEAD). The fenced supervisor terminally fences
        # the drifted pending row at the send edge.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_unfenced_discovery(
            MappingRedmineJournalSource(
                payload=_journals(("110", _req(OLD_HEAD)), ("120", _res(head=OLD_HEAD, req="110")))
            ),
            wsid,
        )
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))  # enqueued while OLD_HEAD was current
        # The current review generation head advanced to CUR_HEAD via a newer review_request (j130).
        advanced = MappingRedmineJournalSource(
            payload=_journals(
                ("110", _req(OLD_HEAD)), ("120", _res(head=OLD_HEAD, req="110")), ("130", _req(CUR_HEAD))
            )
        )
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=advanced, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

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

    # -- Full-surface adversarial matrix (escalation j#81497): req / head / source_sequence
    #    at the discovery, persisted-payload, and send-edge boundaries, all through the supervisor ---

    def _supervise_current(self, source, *, anchor=ANCHOR):
        """Own+lease a fresh workspace and run the fenced supervisor over ``source``; return report."""
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=source, transport=self._transport, anchor=anchor)
        return wsid, supervisor.run_once()

    def test_fs_discovery_req_mismatch_refused_via_supervisor(self) -> None:
        # req MISMATCH at DISCOVERY: the result marker declares req=999 != correlated request j110.
        mismatch = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head=CUR_HEAD, req="999")))
        )
        _wsid, report = self._supervise_current(mismatch)
        self.assertEqual(self._transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_REVIEW_REQUEST_UNCONFIRMED, self._refusals(report))

    def test_fs_preexisting_live_req_missing_terminal_via_supervisor(self) -> None:
        # req MISSING at the SEND EDGE: a row enqueued with a valid live req, then the LIVE review_result
        # marker declares NO req (broken action identity). The send edge terminally fences the row.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_unfenced_discovery(_current_round_source(), wsid)  # recorded req=110
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        broken = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head=CUR_HEAD, req=None)))
        )
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=broken, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_preexisting_live_req_mismatch_terminal_via_supervisor(self) -> None:
        # req MISMATCH at the SEND EDGE: the LIVE review_result marker's declared req drifted to 999.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_unfenced_discovery(_current_round_source(), wsid)  # recorded req=110
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        drifted = MappingRedmineJournalSource(
            payload=_journals(("110", _req(CUR_HEAD)), ("120", _res(head=CUR_HEAD, req="999")))
        )
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=drifted, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_preexisting_malformed_head_terminal_via_supervisor(self) -> None:
        # head MALFORMED at the SEND EDGE: a row enqueued (unfenced) with a non-full-hex recorded head
        # reaches a terminal disposition through the fenced supervisor.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_unfenced_discovery(
            MappingRedmineJournalSource(
                payload=_journals(("110", _req("deadbeef")), ("120", _res(head="deadbeef", req="110")))
            ),
            wsid,
        )
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_source_sequence_is_the_review_result_journal(self) -> None:
        # source_sequence: the delivered row is keyed on the PROVIDER's review_result journal id (j120),
        # and a re-discovery of the same source_sequence is idempotent (no duplicate delivery).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        delivered = self._return_rows([CALLBACK_DELIVERED])
        self.assertEqual([r.journal for r in delivered], ["120"])  # source_sequence = review_result journal
        supervisor.run_once()  # same source_sequence re-discovered -> idempotent
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)

    # -- F2 producer->consumer vertical slice (ACTUAL CLI emits v2) -----------

    def _run_emit_gate_cli(self, writer, gate, *extra):
        """Drive the ACTUAL `workflow callbacks --emit-gate` CLI (not emit_gate_record) against a
        recording transport, hermetic on this test's temp home + store; return ``rc``."""
        import os

        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["workflow", "callbacks", "--emit-gate", "--issue", ISSUE, "--gate", gate,
             "--store-path", str(self.store_path), *extra, "--json"]
        )
        with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
            "redmine_note_transport.redmine_delivery_transport_from_env",
            return_value=writer,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.cli_workflow_callbacks._activate_supervisor_process",
            lambda: None,
        ):
            return args.func(args)

    def _emit_gate_via_cli(self, writer, gate, *extra):
        """As :meth:`_run_emit_gate_cli` but asserts a successful record and returns the posted note."""
        rc = self._run_emit_gate_cli(writer, gate, *extra)
        self.assertEqual(rc, 0, f"--emit-gate {gate} should record")
        return writer.notes[-1]

    def _write_observation(self, *, req, head, name="reviewgen.json"):
        """Write a durable review-generation observation JSON for an approval admission."""
        import json as _json

        p = self.home / name
        p.write_text(
            _json.dumps({
                "issue": ISSUE, "review_request_journal": req, "target_head": head,
                "source_request_seq": 10, "decisions": [{"kind": "approval", "seq": 11}],
            }),
            encoding="utf-8",
        )
        return str(p)

    def test_producer_cli_e2e_current_row_delivers_exactly_once(self) -> None:
        # j#81496 F2: the ACTUAL --emit-gate CLI emits the v2 marker fields (head + req + conclusion),
        # so a fresh structured review round flows CLI -> captured Redmine journal -> supervisor and
        # delivers the current review_return exactly once. The vertical slice is closed at the real CLI.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        writer = _CapturingNoteTransport()
        req_note = self._emit_gate_via_cli(writer, "review_request", "--target-head", CUR_HEAD)
        res_note = self._emit_gate_via_cli(
            writer, "review_result", "--target-head", CUR_HEAD,
            "--review-request-journal", "110", "--review-decision", "changes_requested",
        )
        # The CLI-emitted review_result marker carries conclusion + head + req (not head/req only).
        self.assertIn("head=" + CUR_HEAD, res_note)
        self.assertIn("req=110", res_note)
        self.assertIn("conclusion=changes_requested", res_note)
        # Redmine assigns the journal ids; assemble the exact source the supervisor re-reads.
        source = MappingRedmineJournalSource(payload={"issue": {"id": ISSUE}, "journals": [
            {"id": "110", "notes": req_note},
            {"id": "120", "notes": res_note},
        ]})
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)
        supervisor.run_once()  # idempotent
        self.assertEqual(len(transport.calls), 1)

    def test_producer_cli_e2e_approval_identity_match_delivers(self) -> None:
        # j#81506 F2: an ACTUAL CLI approval whose generation-admission observation exact-matches the
        # marker write target records `conclusion=approved:head:req` and delivers exactly once.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        writer = _CapturingNoteTransport()
        obs = self._write_observation(req="110", head=CUR_HEAD)
        req_note = self._emit_gate_via_cli(writer, "review_request", "--target-head", CUR_HEAD)
        res_note = self._emit_gate_via_cli(
            writer, "review_result", "--target-head", CUR_HEAD, "--review-request-journal", "110",
            "--review-decision", "approval", "--review-generation-json", obs, "--consumer-id", "reviewer-A",
        )
        self.assertIn("conclusion=approved", res_note)
        source = MappingRedmineJournalSource(payload={"issue": {"id": ISSUE}, "journals": [
            {"id": "110", "notes": req_note}, {"id": "120", "notes": res_note},
        ]})
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(wsid=wsid, source=source, transport=transport, anchor=ANCHOR)
        supervisor.run_once()
        self.assertEqual([t[1].lane for t in transport.calls], [LANE])
        self.assertEqual(len(self._return_rows([CALLBACK_DELIVERED])), 1)

    def test_producer_cli_e2e_approval_identity_mismatch_writes_nothing(self) -> None:
        # j#81506 F2: an approval lease held for a DIFFERENT generation (observation head=OLD/req=999)
        # must NOT write this generation's approved marker (write flags head=CUR/req=110). The CLI
        # refuses (write 0), so no journal is captured and the supervisor delivers nothing.
        writer = _CapturingNoteTransport()
        obs = self._write_observation(req="999", head=OLD_HEAD)  # a DIFFERENT generation's lease
        rc = self._run_emit_gate_cli(
            writer, "review_result", "--target-head", CUR_HEAD, "--review-request-journal", "110",
            "--review-decision", "approval", "--review-generation-json", obs, "--consumer-id", "reviewer-A",
        )
        self.assertEqual(rc, 1)  # identity mismatch -> write refused
        self.assertEqual(writer.notes, [])  # nothing written

    # -- Full-surface conclusion boundary matrix (escalation j#81497) ---------

    def test_fs_discovery_missing_conclusion_refused_via_supervisor(self) -> None:
        # conclusion MISSING at DISCOVERY: the review_result marker omits conclusion (-> pending intake).
        _wsid, report = self._supervise_current(_current_round_source(conclusion=None))
        self.assertEqual(self._transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_MISSING_REVIEW_CONCLUSION, self._refusals(report))

    def test_fs_preexisting_live_conclusion_missing_terminal_via_supervisor(self) -> None:
        # conclusion MISSING at the SEND EDGE: a row FENCED-enqueued with a full v2 payload
        # (req+head+conclusion=approved), then the LIVE marker's conclusion is removed (-> pending). The
        # payload's head/req still match, so the CONCLUSION fence is the load-bearing terminal (j#81512 F2).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(_current_round_source(), wsid)  # payload conclusion=approved
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(conclusion=None), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_preexisting_live_conclusion_drift_terminal_via_supervisor(self) -> None:
        # conclusion DRIFT at the SEND EDGE: the row FENCED-enqueued with conclusion=approved (full v2
        # payload), the LIVE marker now says changes_requested. head/req match -> the CONCLUSION fence
        # is the load-bearing terminal (j#81512 F2).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(_current_round_source(conclusion="approved"), wsid)
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(conclusion="changes_requested"),
            transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_newer_malformed_conclusion_shadows_old_valid_via_supervisor(self) -> None:
        # j#81512 F1 case A: an OLD valid approved result (j120) followed by a NEWER review_result (j130)
        # with a malformed conclusion. The malformed newer result stays recognized (pending) and
        # SHADOWS the old one, so nothing delivers — the old result is not sent as "current".
        malformed_newer = MappingRedmineJournalSource(payload=_journals(
            ("110", _req(CUR_HEAD)),
            ("120", _res(head=CUR_HEAD, req="110", conclusion="approved")),
            ("130", _res(head=CUR_HEAD, req="110", conclusion="bogus")),  # newer, out-of-vocab
        ))
        _wsid, report = self._supervise_current(malformed_newer)
        self.assertEqual(self._transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        # j130 (malformed -> pending) is the latest; its return is refused for a non-explicit conclusion.
        self.assertIn(RETURN_MISSING_REVIEW_CONCLUSION, self._refusals(report))

    def test_fs_same_journal_conflicting_conclusion_ambiguous_via_supervisor(self) -> None:
        # j#81512 F1 case B: the SAME provider journal (j120) carries two review_result markers with
        # disagreeing conclusions. The identity is ambiguous -> fail-closed, nothing delivers.
        conflicting = MappingRedmineJournalSource(payload=_journals(
            ("110", _req(CUR_HEAD)),
            ("120",
             _res(head=CUR_HEAD, req="110", conclusion="approved") + "\n"
             + _res(head=CUR_HEAD, req="110", conclusion="changes_requested")),
        ))
        _wsid, report = self._supervise_current(conflicting)
        self.assertEqual(self._transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_AMBIGUOUS_REVIEW_IDENTITY, self._refusals(report))

    def _conflicting_request_head_source(self):
        # j#81518 F1: review_request j110 carries two markers with disagreeing heads.
        return MappingRedmineJournalSource(payload=_journals(
            ("110", _req(CUR_HEAD) + "\n" + _req(OLD_HEAD)),
            ("120", _res(head=CUR_HEAD, req="110", conclusion="approved")),
        ))

    def test_fs_same_journal_conflicting_request_head_ambiguous_via_supervisor(self) -> None:
        # j#81518 F1: the SAME review_request journal carries conflicting heads -> ambiguous -> fresh
        # discovery refuses (0-enqueue), nothing delivers.
        _wsid, report = self._supervise_current(self._conflicting_request_head_source())
        self.assertEqual(self._transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertIn(RETURN_AMBIGUOUS_REVIEW_IDENTITY, self._refusals(report))

    def test_fs_preexisting_row_request_head_conflict_terminal_via_supervisor(self) -> None:
        # j#81518 F1 (backlog): a valid v2 row was FENCED-enqueued while the request head was unique;
        # the request journal then gains a conflicting head. The send-edge head fence terminals the row
        # (current review head collapses to blank on the ambiguous request).
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(_current_round_source(), wsid)  # valid full v2 payload
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=self._conflicting_request_head_source(), transport=transport, anchor=ANCHOR,
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertTrue(self._return_rows([CALLBACK_UNCERTAIN]))

    def test_fs_final_reread_conflict_after_valid_snapshot_sends_zero(self) -> None:
        # j#81518 F2 (TOCTOU): a valid v2 row is enqueued; the supervisor's SNAPSHOT source is still
        # valid (send-edge fence passes), but the SENDER's final live round-fence re-read observes a
        # same-result-journal conflict. review_return_is_current itself fails closed -> send 0.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(_current_round_source(), wsid)
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        conflict_reread = MappingRedmineJournalSource(payload=_journals(
            ("110", _req(CUR_HEAD)),
            ("120",
             _res(head=CUR_HEAD, req="110", conclusion="approved") + "\n"
             + _res(head=CUR_HEAD, req="110", conclusion="changes_requested")),
        ))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
            round_fence_source=conflict_reread,  # the final live re-read disagrees with the snapshot
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])  # the irreversible send never fires
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])

    def _final_reread_drift_is_terminal_zero_send(self, recorded_source, reread_source):
        """A valid v2 row is enqueued from ``recorded_source`` and the supervisor SNAPSHOT stays valid,
        but the SENDER's final live round-fence re-reads ``reread_source`` — a single-marker head /
        conclusion DRIFT that a readable provider deterministically supersedes. Assert (j#81525 F1 +
        R8-F1): the irreversible send never fires AND the row is TERMINAL zero-send (uncertain, retry 0,
        no delivered, no retryable-pending) — not a bounded-retry pending row that keeps the backlog."""
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(recorded_source, wsid)
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))
        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=recorded_source, transport=transport, anchor=ANCHOR,
            round_fence_source=reread_source,  # the final live re-read drifts from the snapshot
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])  # R8-F1: not retryable-pending
        terminal = self._return_rows([CALLBACK_UNCERTAIN])
        self.assertEqual(len(terminal), 1)  # terminal zero-send row
        self.assertEqual([r.attempts for r in terminal], [0])  # retry 0 — never bounded-retried

    def test_fs_final_reread_head_drift_is_terminal_zero_send(self) -> None:
        # j#81525 F1 + R8-F1: the final re-read's single review_result reviewed a DIFFERENT head than
        # recorded -> deterministic supersession -> terminal zero-send (retry 0), not pending.
        self._final_reread_drift_is_terminal_zero_send(
            _current_round_source(),
            MappingRedmineJournalSource(payload=_journals(
                ("110", _req(OLD_HEAD)), ("120", _res(head=OLD_HEAD, req="110", conclusion="approved")),
            )),
        )

    def test_fs_final_reread_conclusion_drift_approved_to_changes_is_terminal(self) -> None:
        # j#81525 F1 + R8-F1: recorded=approved, final re-read=changes_requested (explicit-to-explicit
        # drift) -> deterministic supersession -> terminal zero-send (retry 0), not pending.
        self._final_reread_drift_is_terminal_zero_send(
            _current_round_source(conclusion="approved"),
            MappingRedmineJournalSource(payload=_journals(
                ("110", _req(CUR_HEAD)),
                ("120", _res(head=CUR_HEAD, req="110", conclusion="changes_requested")),
            )),
        )

    def test_fs_final_reread_conclusion_drift_changes_to_approved_is_terminal(self) -> None:
        # j#81525 F1 + R8-F1 (reverse direction): recorded=changes_requested, final re-read=approved ->
        # deterministic supersession -> terminal zero-send (retry 0), not pending. Fixes BOTH drift
        # directions so a stale approval and a stale changes_requested are equally terminal.
        self._final_reread_drift_is_terminal_zero_send(
            _current_round_source(conclusion="changes_requested"),
            MappingRedmineJournalSource(payload=_journals(
                ("110", _req(CUR_HEAD)),
                ("120", _res(head=CUR_HEAD, req="110", conclusion="approved")),
            )),
        )

    def test_fs_final_reread_unreadable_provider_is_retryable_not_terminal(self) -> None:
        # R8-F1 (the discriminating case): a valid v2 row, valid snapshot, but the SENDER's final
        # round-fence provider read RAISES transiently. This must stay RETRYABLE (pending, bounded
        # retry) — a genuinely-current callback must not be terminally dropped by a transient outage.
        wsid = self._register_ws()
        self._own_and_lease(wsid)
        self._enqueue_via_fenced_discovery(_current_round_source(), wsid)
        self.assertTrue(self._return_rows([CALLBACK_PENDING]))

        class _RaisingSource:
            def read_entries(self, *a, **k):
                raise RuntimeError("transient provider outage")

        transport = _CapturingTransport()
        supervisor = self._build_supervisor(
            wsid=wsid, source=_current_round_source(), transport=transport, anchor=ANCHOR,
            round_fence_source=_RaisingSource(),
        )
        supervisor.run_once()
        self.assertEqual(transport.calls, [])  # still zero-send (fail-closed)
        self.assertEqual(self._return_rows([CALLBACK_DELIVERED]), [])
        self.assertEqual(self._return_rows([CALLBACK_UNCERTAIN]), [])  # NOT terminally dropped
        pending = self._return_rows([CALLBACK_PENDING])
        self.assertEqual(len(pending), 1)  # retryable — the current callback survives the outage
        self.assertEqual([r.attempts for r in pending], [1])  # bounded-retry bumped attempts

    def test_producer_cli_fail_closed_validation(self) -> None:
        # j#81487 F2: the canonical --emit-gate writer refuses to emit a head-less / malformed / req-less
        # review marker rather than write one the callback fence would reject.
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (
            _review_gate_marker_fields,
        )

        def _fields(gate, **kw):
            ns = argparse.Namespace(
                target_head=kw.get("head"), review_request_journal=kw.get("req"),
                review_decision=kw.get("decision"),
            )
            return _review_gate_marker_fields(ns, gate)

        self.assertEqual(_fields("review_request")[1], "review_marker_missing_target_head")
        self.assertEqual(_fields("review_request", head="deadbeef")[1], "review_marker_malformed_target_head")
        self.assertEqual(_fields("review_result", head=CUR_HEAD)[1], "review_marker_missing_review_request_journal")
        # A review_result carries head + req + conclusion (v2). changes_requested -> changes_requested.
        fields, refusal = _fields("review_result", head=CUR_HEAD, req="110", decision="changes_requested")
        self.assertIsNone(refusal)
        self.assertEqual(fields, {"target_head": CUR_HEAD, "review_request_journal": "110", "conclusion": "changes_requested"})
        # An unspecified / approval decision -> conclusion approved.
        self.assertEqual(_fields("review_result", head=CUR_HEAD, req="110")[0]["conclusion"], "approved")
        self.assertEqual(_fields("review_result", head=CUR_HEAD, req="110", decision="approval")[0]["conclusion"], "approved")
        # A non-review gate carries no v2 fields (unchanged).
        self.assertEqual(_fields("implementation_done"), ({}, None))


class _CapturingNoteTransport:
    """A fake :class:`NoteWriteTransport` that records every posted note (F2 producer E2E)."""

    def __init__(self) -> None:
        self.notes: list = []

    def post_issue_note(self, issue_id: str, notes: str) -> str:
        self.notes.append(notes)
        return f"redmine:issue={issue_id}"


if __name__ == "__main__":
    unittest.main()
