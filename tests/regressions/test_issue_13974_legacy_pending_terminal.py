"""Regression pin: legacy pending review_return backlog terminal convergence (Redmine #13974 R2).

Fixed defect (installed-a9 live finding j#81622): a PRE-EXISTING pending ``review_return`` row whose
owning lane later hibernated / was superseded — and whose LEGACY payload carries only
``review_request_journal`` (no head / conclusion, an a8-era row like #13933) — stayed RETRYABLE forever
instead of converging to a terminal zero-send. Two entry paths left it pending:

- the public ``workflow callbacks --deliver`` (source-less + unfenced) merely attempt-and-retried it,
  bumping ``attempts`` on every pass (``SEND_NOT_SENT`` -> ``mark_retry_or_dead``);
- the global ``workflow supervisor --run-once`` only ran a fenced deliver pass for issues in the LIVE
  active-pane roster, so a hibernated issue's backlog row was never revisited.

The fix drains a workspace's OWN pending partition (issues no longer in any active roster) through the
SAME action-time send-edge fence, so a readable durable provider that shows the round is
previous-generation / hibernated-owner / identity-drifted converges the row to ``mark_uncertain``
(terminal zero-send, retry 0, attempts unchanged). A merely-UNREADABLE provider stays retryable
(deterministic-stale vs transient-unreadable, never conflated). Foreign-partition rows are untouched.

Exercised through the REAL machinery (real outbox, real lifecycle owning-lane authority; only the
Redmine source + sender faked): direct drain, the full supervisor run-once with an empty roster,
transient retention, restart/replay, and the CLI ``--deliver`` routing.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer, LaneLifecycleKey
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore, supervisor_lease_path
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
    WorkflowRuntimeStore,
    workflow_runtime_store_path,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_callbacks as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    DEFAULT_CALLBACK_ROUTE,
    discover_review_returns,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    workspace_callback_review_return as rr,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (
    BacklogDrainOutcome,
    drain_review_return_backlog,
    owning_lane_binding,
    owning_lane_generation_reader,
    review_round_send_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackendNeutralTargetResolver,
    BackgroundServiceCallbackSender,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
    HandoffDeliveryResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    default_workspaces,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
    SEND_NOT_SENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    render_dispatch_marker,
    render_workflow_event_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    encode_review_return_payload,
    is_review_return_route,
    review_return_callback_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)

NOW = "2026-07-13T00:00:00+00:00"
WS = "wsBacklog"
ISSUE = "13974"
LANE = "issue_13974"
CUR_HEAD = "c" * 40
OLD_HEAD = "d" * 40
#: The dispatch anchor journal for the current generation (lane_generation 1). The old round (j10/j20)
#: precedes it; the current round (j110/j120) follows it. Journal ids are monotonic (chronological).
IR_JOURNAL = "100"


def _journals(*entries):
    return {"issue": {"id": ISSUE}, "journals": [{"id": jid, "notes": notes} for jid, notes in entries]}


def _req(head):
    return render_workflow_event_marker("review_request", target_head=head)


def _res(conclusion="approved", head=None, req=None):
    return render_workflow_event_marker(
        "review_result", conclusion=conclusion, target_head=head, review_request_journal=req
    )


def _ir(gen=1):
    return (IR_JOURNAL, render_dispatch_marker(LANE, gen))


def _old_round_source(*extra):
    """OLD generation round (no IR marker): request j10 -> result j20, still the newest review marker."""
    return MappingRedmineJournalSource(
        payload=_journals(("10", _req(OLD_HEAD)), ("20", _res(head=OLD_HEAD, req="10")), *extra)
    )


def _previous_generation_source():
    """An active current generation (IR at j100) whose only review round (j10/j20) PREDATES the anchor."""
    return MappingRedmineJournalSource(
        payload=_journals(("10", _req(OLD_HEAD)), ("20", _res(head=OLD_HEAD, req="10")), _ir())
    )


def _current_round_source():
    """CURRENT generation: IR at j100, request j110 -> result j120 (both after the anchor)."""
    return MappingRedmineJournalSource(
        payload=_journals(
            _ir(), ("110", _req(CUR_HEAD)), ("120", _res(conclusion="approved", head=CUR_HEAD, req="110"))
        )
    )


class _RaisingSource:
    """A provider whose read RAISES — the transient-unreadable case (never a deterministic stale)."""

    def read_entries(self, issue_id):
        raise RuntimeError("transient redmine outage")


class _RecordingSender:
    """A sender that records the rows it is asked to deliver (it must NOT be asked for a fenced row)."""

    def __init__(self, outcome=SEND_DELIVERED):
        self.calls = []
        self._outcome = outcome

    def __call__(self, row):
        self.calls.append(row)
        return self._outcome


def _cli_args(**over) -> argparse.Namespace:
    base = dict(
        json=False, store_path=None, sweep=False, ingest=False, deliver=True, run_once=False,
        watch=False, candidate=None, redmine_json=None, poll=False, source_issue=None,
        since=None, cursor=None, limit=32,
    )
    base.update(over)
    return argparse.Namespace(**base)


class LegacyPendingTerminalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store_path = workflow_runtime_store_path(self.home)
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.life = LaneLifecycleStore(home=self.home)
        self.lease = SupervisorLeaseStore(path=supervisor_lease_path(self.home))

    # -- helpers ----------------------------------------------------------

    def _declare_owner(self, lane=LANE, wsid=WS, journal="100"):
        self.life.declare_active(
            LaneLifecycleKey(wsid, lane),
            decision=DecisionPointer("redmine", ISSUE, journal), issue_id=ISSUE, now=NOW,
        )

    def _legacy_row(self, *, req="10", journal="20", lane=LANE, wsid=WS, gen="1", payload=None):
        """Enqueue a PRE-EXISTING pending review_return row shaped exactly like the a8-era #13933 row:
        a legacy payload carrying ONLY ``review_request_journal`` (no head / conclusion)."""
        key = CallbackOutboxKey(
            source="redmine", issue=ISSUE, journal=journal, normalized_gate="review",
            callback_route=review_return_callback_route(lane), workspace_id=wsid,
        )
        self.outbox.enqueue(
            key, initial_state=CALLBACK_PENDING,
            payload=payload if payload is not None else encode_review_return_payload(req),
            target_lane=lane, target_receiver="codex", target_generation=gen, now=NOW,
        )
        return key

    def _return_rows(self, states, wsid=WS):
        return [
            r for r in self.outbox.read(states=states)
            if is_review_return_route(r.callback_route) and r.workspace_id == wsid
        ]

    def _drain(self, source, sender, wsid=WS, **kw):
        return drain_review_return_backlog(
            self.outbox, wsid, source=source, sender=sender,
            lifecycle_store=self.life, route=DEFAULT_CALLBACK_ROUTE, **kw
        )

    # -- direct drain: deterministic terminal -----------------------------

    def test_direct_drain_terminalizes_hibernated_legacy_row(self) -> None:
        # The installed-a9 repro: a legacy row whose owning lane is hibernated (resolve_owner unresolved)
        # -> the drain cannot pin a current generation anchor -> UNRESOLVED_ANCHOR terminal. Zero-send,
        # marked uncertain (retry 0, attempts unchanged), NOT a bounded-retry pending row.
        self._legacy_row()  # no owner declared -> hibernated
        sender = _RecordingSender()
        outcome = self._drain(_old_round_source(), sender)
        self.assertEqual(sender.calls, [])  # zero-send: the fence fires before the sender
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])  # send=0 / pending=0
        terminal = self._return_rows([CALLBACK_UNCERTAIN])
        self.assertEqual(len(terminal), 1)  # terminal=1
        self.assertEqual([r.attempts for r in terminal], [0])  # attempts unchanged
        self.assertEqual(outcome.fenced, 1)

    def test_direct_drain_terminalizes_previous_generation_row(self) -> None:
        # An ACTIVE current generation (IR at j100) whose backlog row answers a round (j10) predating the
        # anchor -> PREVIOUS_GENERATION terminal via the real anchor resolution (owner + IR marker).
        self._declare_owner()
        self._legacy_row(req="10")
        sender = _RecordingSender()
        outcome = self._drain(_previous_generation_source(), sender)
        self.assertEqual(sender.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        self.assertEqual(len(self._return_rows([CALLBACK_UNCERTAIN])), 1)
        self.assertEqual([r.attempts for r in self._return_rows([CALLBACK_UNCERTAIN])], [0])
        self.assertEqual(outcome.fenced, 1)

    def test_direct_drain_leaves_current_generation_row_deliverable(self) -> None:
        # Adversarial safety witness (guard-that-doesn't-over-fence): a CURRENT-generation row whose full
        # v2 identity (req/head/conclusion, from fenced discovery) matches the live markers is NOT fenced
        # by the drain — the sender IS invoked and it delivers. The drain converges STALE rows only.
        self._declare_owner()
        source = _current_round_source()
        owner = owning_lane_binding(WS, ISSUE, RoleProviderBinding.default(), lifecycle_store=self.life)
        candidates, _ = discover_review_returns(
            source, ISSUE, owner, workspace_id=WS, dispatch_anchor_journal=IR_JOURNAL
        )
        self.assertEqual(len(candidates), 1, "fenced discovery should emit a full-identity current row")
        CallbackOutboxProcessor(self.outbox, source, workspace_id=WS).ingest(candidates, now=NOW)
        sender = _RecordingSender(outcome=SEND_DELIVERED)
        outcome = self._drain(source, sender)
        self.assertEqual(len(sender.calls), 1)  # NOT fenced -> the sender is asked to deliver
        self.assertEqual(self._return_rows([CALLBACK_UNCERTAIN]), [])  # not terminalized
        self.assertEqual(outcome.fenced, 0)
        self.assertEqual(outcome.delivered, 1)

    # -- transient vs deterministic (the split correction 3 requires) -----

    def test_transient_unreadable_provider_leaves_row_pending(self) -> None:
        # A merely-UNREADABLE provider (the read raises) must NOT terminalize a possibly-current round:
        # the row stays pending (retryable), attempts UNCHANGED, and the drain reports it skipped.
        self._legacy_row()
        sender = _RecordingSender()
        outcome = self._drain(_RaisingSource(), sender)
        self.assertEqual(sender.calls, [])
        pending = self._return_rows([CALLBACK_PENDING])
        self.assertEqual(len(pending), 1)  # still retryable
        self.assertEqual([r.attempts for r in pending], [0])  # attempts unchanged — never delivered
        self.assertEqual(self._return_rows([CALLBACK_UNCERTAIN]), [])  # NOT deterministically terminal
        self.assertEqual(outcome.transient_skipped, 1)
        self.assertEqual(outcome.fenced, 0)

    # -- full supervisor run-once (global one-shot) -----------------------

    def _register_ws(self, name="repoBacklog"):
        repo = self.home / name
        repo.mkdir()
        return register_workspace(repo, home=self.home).record.workspace_id

    def _backlog_supervisor(self, *, wsid, source, sender, roster=(), holder="superF"):
        """A real WorkspaceCallbackSupervisor with the #13974 R2 backlog drain wired and a controllable
        roster, so the empty-roster (all-hibernated) path is exercised end-to-end."""
        def sender_fn(ws):
            return sender

        def backlog_drain_fn(wid, *, source, sender, skip_issues, lease_guard_fn):
            return drain_review_return_backlog(
                self.outbox, wid, source=source, sender=sender, lifecycle_store=self.life,
                route=DEFAULT_CALLBACK_ROUTE, lease_guard_fn=lease_guard_fn, skip_issues=skip_issues,
            )

        return WorkspaceCallbackSupervisor(
            holder=holder, lease_store=self.lease, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [w for w in default_workspaces(home=self.home) if w.workspace_id == wsid],
            roster_fn=lambda ws: (tuple(roster), ""),
            redmine_source_fn=lambda ws: source,
            sender_fn=sender_fn,
            binding_fn=lambda ws: RoleProviderBinding.default(),
            owner_binding_fn=lambda w, i, b: owning_lane_binding(w, i, b, lifecycle_store=self.life),
            release_after=False, clock=lambda: NOW,
            candidate_fence_fn=lambda w, i, s: IR_JOURNAL,
            backlog_drain_fn=backlog_drain_fn,
        )

    def test_supervisor_run_once_drains_backlog_with_empty_roster(self) -> None:
        # The global one-shot repro: the issue is NOT in the active roster (its lane hibernated), so the
        # active-issue pass never revisits it — but the own-workspace backlog drain converges it under the
        # SAME lease, so `workflow supervisor --run-once` reports a terminal fence.
        wsid = self._register_ws()
        self._legacy_row(wsid=wsid)
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        sender = _RecordingSender()
        report = self._backlog_supervisor(wsid=wsid, source=_old_round_source(), sender=sender, roster=())
        result = report.run_once()
        self.assertEqual(sender.calls, [])
        self.assertEqual(self._return_rows([CALLBACK_PENDING], wsid=wsid), [])
        self.assertEqual(len(self._return_rows([CALLBACK_UNCERTAIN], wsid=wsid)), 1)
        self.assertEqual(result.backlog_fenced, 1)

    def test_supervisor_skips_already_supervised_issue_in_backlog(self) -> None:
        # The backlog drain skips issues the active-issue pass already handled (no double-processing): with
        # ISSUE in the roster, `skip_issues` excludes it, so the backlog drain fences nothing extra.
        wsid = self._register_ws()
        self._declare_owner(wsid=wsid)
        self._legacy_row(wsid=wsid)
        self.lease.acquire(wsid, "superF", now=NOW, ttl_seconds=600)
        report = self._backlog_supervisor(
            wsid=wsid, source=_current_round_source(), sender=_RecordingSender(), roster=(ISSUE,)
        )
        result = report.run_once()
        self.assertEqual(result.backlog_fenced, 0)  # the active pass owns this issue; the drain skips it

    # -- restart / replay -------------------------------------------------

    def test_replay_keeps_terminal_and_never_resurrects(self) -> None:
        # A terminally-fenced (uncertain) row is not pending, so a SECOND drain pass (a restart / replay)
        # never re-claims it — it stays terminal, attempts unchanged, never resurrected as pending.
        self._legacy_row()
        for _ in range(2):
            self._drain(_old_round_source(), _RecordingSender())
        self.assertEqual(self._return_rows([CALLBACK_PENDING]), [])
        terminal = self._return_rows([CALLBACK_UNCERTAIN])
        self.assertEqual(len(terminal), 1)
        self.assertEqual([r.attempts for r in terminal], [0])

    # -- foreign-workspace partition safety -------------------------------

    def test_foreign_partition_row_is_untouched(self) -> None:
        # Foreign-partition-safe: a row in ANOTHER workspace's partition is never read, claimed, or fenced
        # by this workspace's drain — it stays exactly pending.
        self._legacy_row(wsid="wsOther")
        outcome = self._drain(_old_round_source(), _RecordingSender(), wsid=WS)
        self.assertEqual(outcome.fenced, 0)
        foreign = [r for r in self.outbox.read(states=[CALLBACK_PENDING]) if r.workspace_id == "wsOther"]
        self.assertEqual(len(foreign), 1)  # untouched
        self.assertEqual([r.attempts for r in foreign], [0])

    # -- load-bearing witness: the pre-fix path leaves it retryable -------

    def test_unfenced_direct_deliver_leaves_row_retryable_the_bug(self) -> None:
        # The exact pre-fix failure: a raw `processor.deliver` (no send_fence_fn) over a source-less null
        # source attempt-and-retries the legacy row (a hibernated target reports not-sent) -> the row stays
        # PENDING with attempts BUMPED. This is the finding; the drain above closes it.
        self._legacy_row()
        proc = CallbackOutboxProcessor(self.outbox, cli._NULL_SOURCE, workspace_id=WS)
        proc.deliver(lambda row: SEND_NOT_SENT, now=NOW)
        pending = self._return_rows([CALLBACK_PENDING])
        self.assertEqual(len(pending), 1)  # never terminal
        self.assertEqual([r.attempts for r in pending], [1])  # bounded-retry bumped attempts — the bug

    # -- CLI --deliver routing --------------------------------------------

    def test_cli_deliver_with_source_routes_to_the_fenced_drain(self) -> None:
        # `workflow callbacks --deliver --poll` (a readable provider) routes to the fenced backlog drain,
        # so the operator command CONVERGES a stale row instead of attempt-and-retrying it.
        captured = {}

        def fake_drain(outbox, workspace_id, *, source, sender, home=None):
            captured["workspace_id"] = workspace_id
            return BacklogDrainOutcome(workspace_id=workspace_id, fenced=1)

        with mock.patch.object(rr, "deliver_workspace_backlog", fake_drain), \
             mock.patch.object(cli, "_optional_journal_source", lambda a: object()), \
             mock.patch.object(cli, "_require_partition_workspace_id", lambda a: WS), \
             mock.patch.object(cli, "_callback_sender", lambda a: (lambda row: SEND_NOT_SENT)), \
             mock.patch.object(cli, "_outbox_from_args", lambda a: self.outbox):
            rc = cli.cmd_workflow_callbacks(_cli_args(deliver=True, poll=True))
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("workspace_id"), WS)  # the drain was invoked with this partition

    def test_cli_deliver_without_source_stays_a_raw_drain(self) -> None:
        # Source-less `--deliver` is unchanged (a deterministic previous-generation judgement is impossible
        # without a provider): it never calls the fenced drain and stays the raw one-send-per-row path.
        self._legacy_row()

        def _must_not_call(*a, **k):  # pragma: no cover - asserts the raw path is taken
            raise AssertionError("source-less --deliver must not route to the fenced drain")

        with mock.patch.object(rr, "deliver_workspace_backlog", _must_not_call), \
             mock.patch.object(cli, "_optional_journal_source", lambda a: None), \
             mock.patch.object(cli, "_require_partition_workspace_id", lambda a: WS), \
             mock.patch.object(cli, "_callback_sender", lambda a: (lambda row: SEND_NOT_SENT)), \
             mock.patch.object(cli, "_outbox_from_args", lambda a: self.outbox):
            rc = cli.cmd_workflow_callbacks(_cli_args(deliver=True, poll=False))
        self.assertEqual(rc, 0)
        pending = self._return_rows([CALLBACK_PENDING])
        self.assertEqual([r.attempts for r in pending], [1])  # raw drain attempt-and-retried


if __name__ == "__main__":
    unittest.main()
