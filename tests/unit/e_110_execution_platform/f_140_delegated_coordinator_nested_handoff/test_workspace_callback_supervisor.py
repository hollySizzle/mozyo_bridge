"""Workspace callback supervisor composition-root tests (Redmine #13683 Phase A).

Pins the Phase A acceptance behaviors with injected fakes (no live registry / Redmine / daemon):

1. a duplicate supervisor (a workspace already leased by another holder) delivers **zero** —
   the callback sender is never invoked and no events are supplied;
2. a leased workspace supplies durable events (so glance/resume stop reporting unknown) AND drains
   the callback outbox partition (one send per handoff-worthy gate);
3. a re-run is idempotent — no duplicate event, no duplicate delivery (the outbox fence);
4. local_wake supervises only wake-named active issues; bounded reconciliation supervises all;
5. a roster read error skips the workspace (fail-closed, not "nothing active");
6. an unreadable Redmine source degrades that issue (still drains the outbox), never aborts.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.supervisor_wake import SupervisorWakeStore
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    WorkflowRuntimeStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
    active_lane_snapshots,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    ISSUE_SOURCE_UNREADABLE,
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    SKIP_LEASE_LOST,
    SKIP_LEASE_REFUSED,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_WAKE,
)

ISSUE = "13683"


def _review_request_payload(issue: str = ISSUE, journal: str = "77065") -> dict:
    """A Redmine issue-detail payload whose journal carries a review_request gate marker."""
    return {
        "issue": {"id": issue},
        "journals": [
            {
                "id": journal,
                "notes": (
                    "## Gate: review_request\n"
                    f"[mozyo:workflow-event:gate=review_request:conclusion=pending]"
                ),
            }
        ],
    }


class _RecordingSender:
    """A one-send callback sender that records each row it is asked to deliver."""

    def __init__(self, outcome: str = SEND_DELIVERED) -> None:
        self.calls: list = []
        self._outcome = outcome

    def __call__(self, row) -> str:
        self.calls.append(row)
        return self._outcome


class WorkspaceCallbackSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_path = self.dir / "supervisor-lease.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.lease_path)
        self.sender = _RecordingSender()
        self.source = MappingRedmineJournalSource(payload=_review_request_payload())
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def _supervisor(self, *, holder="superX", roster=(ISSUE,), roster_error="", source="real"):
        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda ws: (tuple(roster), roster_error),
            redmine_source_fn=lambda ws: (self.source if source == "real" else None),
            sender_fn=lambda ws: self.sender,
            clock=lambda: "2026-07-13T00:00:00+00:00",
        )

    # -- acceptance 1: duplicate supervisor zero-delivery ------------------

    def test_duplicate_supervisor_delivers_nothing(self) -> None:
        # Another supervisor already holds wsA's lease (live, future expiry).
        self.lease_store.acquire("wsA", "otherSuper", now="2026-07-13T00:00:00+00:00", ttl_seconds=600)
        report = self._supervisor(holder="superX").run_once()
        ws = report.workspaces[0]
        self.assertFalse(ws.lease_acquired)
        self.assertEqual(ws.skipped_reason, SKIP_LEASE_REFUSED)
        self.assertEqual(self.sender.calls, [])  # zero delivery
        self.assertEqual(self.store.read_events(), ())  # no events supplied
        self.assertEqual(report.workspaces_skipped, 1)

    # -- acceptance 2: event supply + callback drain ----------------------

    def test_supplies_durable_events_and_drains_callback(self) -> None:
        report = self._supervisor().run_once()
        ws = report.workspaces[0]
        self.assertTrue(ws.lease_acquired)
        self.assertEqual(ws.supervised_issues, (ISSUE,))
        self.assertGreaterEqual(ws.events_supplied, 1)  # workflow_events appended (glance/resume supply)
        self.assertEqual(len(self.sender.calls), 1)  # one send for the review_request gate
        self.assertEqual(ws.delivered, 1)
        # The outbox row is delivered (partitioned to wsA).
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].workspace_id, "wsA")

    def test_supplied_events_resolve_glance_unknown(self) -> None:
        # Before the sweep, glance with no Redmine source folds the lane to a degraded unknown.
        before = active_lane_snapshots([(ISSUE, "laneA")], redmine_source=None, store=self.store)
        self.assertFalse(before.snapshots[0].durable_facts_available)
        # After the sweep, the store advisory cache carries the folded gate facts.
        self._supervisor().run_once()
        after = active_lane_snapshots([(ISSUE, "laneA")], redmine_source=None, store=self.store)
        self.assertTrue(after.snapshots[0].durable_facts_available)  # no longer unknown
        self.assertEqual(after.snapshots[0].signal.latest_gate, "review_request")

    # -- acceptance 3: idempotent replay ----------------------------------

    def test_replay_is_idempotent(self) -> None:
        sup = self._supervisor()
        sup.run_once()
        self.assertEqual(len(self.sender.calls), 1)
        events_after_first = len(self.store.read_events())
        # Second sweep: the durable anchor dedups the event, the outbox fence blocks re-delivery.
        report2 = sup.run_once()
        self.assertEqual(len(self.sender.calls), 1)  # NOT re-sent
        self.assertEqual(report2.workspaces[0].events_supplied, 0)  # NOT re-appended
        self.assertEqual(report2.workspaces[0].delivered, 0)
        self.assertEqual(len(self.store.read_events()), events_after_first)

    # -- acceptance 4: wake modes -----------------------------------------

    def test_local_wake_supervises_only_wake_named_issue(self) -> None:
        sup = self._supervisor(roster=(ISSUE, "13684"))
        report = sup.run_once(mode=SUPERVISION_LOCAL_WAKE, wake_hints=[("wsA", ISSUE)])
        self.assertEqual(report.workspaces[0].supervised_issues, (ISSUE,))  # not 13684

    def test_bounded_reconciliation_supervises_full_roster(self) -> None:
        sup = self._supervisor(roster=(ISSUE, "13684"))
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(set(report.workspaces[0].supervised_issues), {ISSUE, "13684"})

    # -- acceptance 5 / 6: fail-closed roster, fail-open source -----------

    def test_roster_error_skips_workspace_fail_closed(self) -> None:
        report = self._supervisor(roster=(), roster_error="active-lane roster enumeration failed").run_once()
        ws = report.workspaces[0]
        self.assertTrue(ws.lease_acquired)  # leased, but supervised nothing
        self.assertEqual(ws.skipped_reason, SKIP_ROSTER_UNREADABLE)
        self.assertEqual(self.sender.calls, [])

    def test_unreadable_source_degrades_issue_but_still_drains(self) -> None:
        report = self._supervisor(source="none").run_once()
        ws = report.workspaces[0]
        self.assertTrue(ws.lease_acquired)
        self.assertEqual(ws.issues[0].error, ISSUE_SOURCE_UNREADABLE)
        self.assertEqual(ws.issues[0].events_supplied, 0)
        # No candidate was discovered (no source), so nothing to deliver — but the pass ran.
        self.assertEqual(self.sender.calls, [])

    def test_lease_is_released_after_sweep(self) -> None:
        self._supervisor().run_once()
        # release_after default True -> the workspace is free for a later invocation.
        self.assertIsNone(self.lease_store.holder_of("wsA"))

    def test_blank_holder_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkspaceCallbackSupervisor(
                holder="  ", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
                workspaces_fn=lambda: [self.ws], roster_fn=lambda ws: ((ISSUE,), ""),
                redmine_source_fn=lambda ws: self.source, sender_fn=lambda ws: self.sender,
            )


class SupervisorLeaseFenceTest(unittest.TestCase):
    """R1-F1: a stale holder whose lease is taken over mid-sweep stops before the next issue."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.source = MappingRedmineJournalSource(payload=_review_request_payload())
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def test_old_holder_stops_delivering_after_ttl_crossing_takeover(self) -> None:
        # A second-issue source so both issues carry a deliverable gate.
        source2 = MappingRedmineJournalSource(payload=_review_request_payload(issue="13684", journal="77066"))

        def source_fn(ws):
            # One combined source that answers both issues (payload keyed by requested issue).
            class _Multi:
                def read_entries(_self, issue_id):
                    p = self.source if str(issue_id) == "13683" else source2
                    return p.read_entries(issue_id)
            return _Multi()

        lease_store = self.lease_store

        class _TakeoverSender:
            """Delivers issue-0, then a SECOND supervisor takes the (now-expired) lease over."""

            def __init__(self) -> None:
                self.calls = []

            def __call__(self, row):
                self.calls.append(row)
                if len(self.calls) == 1:
                    # now is past superX's expiry (00:00:00 + ttl 100 = 00:01:40) -> takeover.
                    lease_store.acquire("wsA", "superB", now="2026-07-13T00:30:00+00:00", ttl_seconds=600)
                return SEND_DELIVERED

        sender = _TakeoverSender()
        # Fresh clock per read: acquire (issue-0 fence) then the issue-1 renew (past takeover time).
        clocks = iter(["2026-07-13T00:00:00+00:00", "2026-07-13T01:00:00+00:00"])
        sup = WorkspaceCallbackSupervisor(
            holder="superX",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda ws: (("13683", "13684"), ""),
            redmine_source_fn=source_fn,
            sender_fn=lambda ws: sender,
            clock=lambda: next(clocks),
            lease_ttl_seconds=100,
        )
        report = sup.run_once()
        w = report.workspaces[0]
        # The renew fence tripped: only issue-0 was delivered; issue-1's side-effects never ran.
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(w.skipped_reason, SKIP_LEASE_LOST)
        self.assertEqual(len(w.issues), 1)  # only the first issue's outcome recorded
        # The new holder owns the lease; superX's holder-conditional release was a no-op.
        self.assertEqual(self.lease_store.holder_of("wsA").holder, "superB")

    def test_slow_multi_issue_sweep_renews_and_keeps_ownership(self) -> None:
        # No takeover: a live owner's renew succeeds across issues, so all issues are supervised.
        source2 = MappingRedmineJournalSource(payload=_review_request_payload(issue="13684", journal="77066"))

        def source_fn(ws):
            class _Multi:
                def read_entries(_self, issue_id):
                    return (self.source if str(issue_id) == "13683" else source2).read_entries(issue_id)
            return _Multi()

        calls = []
        clocks = iter([f"2026-07-13T00:00:{i:02d}+00:00" for i in range(0, 60, 5)])
        sup = WorkspaceCallbackSupervisor(
            holder="superX", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [self.ws], roster_fn=lambda ws: (("13683", "13684"), ""),
            redmine_source_fn=source_fn, sender_fn=lambda ws: (lambda row: calls.append(row) or SEND_DELIVERED),
            clock=lambda: next(clocks), lease_ttl_seconds=100,
        )
        report = sup.run_once()
        w = report.workspaces[0]
        self.assertEqual(w.skipped_reason, "")
        self.assertEqual(len(w.issues), 2)  # both issues supervised (renew kept ownership)
        self.assertEqual(len(calls), 2)


class SupervisorWakeConsumeTest(unittest.TestCase):
    """R1-F2: the supervisor drains the durable wake queue and consumes it as local_wake."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "lease.sqlite")
        self.wake_store = SupervisorWakeStore(path=self.dir / "wake.sqlite")
        self.source = MappingRedmineJournalSource(payload=_review_request_payload())
        self.sender = _RecordingSender()
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))

    def _supervisor(self):
        return WorkspaceCallbackSupervisor(
            holder="superX", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [self.ws], roster_fn=lambda ws: (("13683", "13684"), ""),
            redmine_source_fn=lambda ws: self.source, sender_fn=lambda ws: self.sender,
            wake_store=self.wake_store, clock=lambda: "2026-07-13T00:00:00+00:00",
        )

    def test_drained_wake_drives_local_wake_and_consumes_queue(self) -> None:
        # A gate-emit-produced wake for one active issue.
        self.wake_store.enqueue("wsA", "13683")
        report = self._supervisor().run_once(mode=SUPERVISION_LOCAL_WAKE)
        w = report.workspaces[0]
        self.assertEqual(w.supervised_issues, ("13683",))  # only the wake-named issue
        # The wake queue was consumed (drained), not left pending.
        self.assertEqual(self.wake_store.pending(), ())

    def test_bounded_reconciliation_also_drains_wake_but_covers_full_roster(self) -> None:
        self.wake_store.enqueue("wsA", "13683")
        report = self._supervisor().run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        w = report.workspaces[0]
        self.assertEqual(set(w.supervised_issues), {"13683", "13684"})  # loss recovery = full roster
        self.assertEqual(self.wake_store.pending(), ())  # still consumed

    def test_absent_wake_store_is_fine(self) -> None:
        sup = WorkspaceCallbackSupervisor(
            holder="superX", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [self.ws], roster_fn=lambda ws: (("13683",), ""),
            redmine_source_fn=lambda ws: self.source, sender_fn=lambda ws: self.sender,
            wake_store=None, clock=lambda: "2026-07-13T00:00:00+00:00",
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(report.workspaces[0].supervised_issues, ("13683",))


if __name__ == "__main__":
    unittest.main()
