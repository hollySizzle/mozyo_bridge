"""Local outbox drain vs provider reconciliation split (Redmine #14150).

Pins the split's acceptance behaviours with injected fakes (no live registry / Redmine / daemon):

1. the LOCAL drain reads local state ONLY — an empty pass AND a safe-pending pass both make ZERO
   ticket-provider reads (the ``redmine_source_fn`` is a spy that fails loudly if the drain touches
   it), yet the safe-pending pass still delivers the locally-attestable coordinator row;
2. a row the drain cannot attest from local state — a mismatched / blank ``enqueue_lane_generation``,
   or a non-coordinator route — is DEFERRED (left pending), never blind-sent and never terminal;
3. the duplicate-supervisor lease fence still fences the drain (a live duplicate owner delivers 0);
4. the lease lifecycle: a terminated bounded holder's retained leases are released by
   ``release_all_leases`` so a fresh run never starves (the j#83437 / j#83443 evidence), while an
   ACTIVE duplicate owner is never evicted (the double-execution fence is preserved);
5. the pure reconciliation-cadence helpers (watermark gate + jitter/backoff) and the pure drain
   selection / attestation helpers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import (
    CallbackOutbox,
    CallbackOutboxKey,
)
from mozyo_bridge.core.state.reconcile_cadence import ReconcileCadenceStore
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
    WorkflowRuntimeStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DRAIN_DEFER_ANCHOR_UNRESOLVED,
    DRAIN_DEFER_NOT_ATTESTABLE,
    SKIP_LEASE_REFUSED,
    SKIP_PASS_BUDGET_SPENT,
    SUPERVISION_LOCAL_DRAIN,
    is_locally_attestable_route,
    reconcile_backoff_seconds,
    select_drain_issues,
    should_reconcile_source,
)

ISSUE = "14150"
CLOCK = "2026-07-20T00:00:00+00:00"


def _boom_source(ws):  # pragma: no cover - invoked only on a bug
    raise AssertionError("the local drain must NEVER resolve a ticket-provider source")


class _RecordingSender:
    def __init__(self, outcome: str = SEND_DELIVERED) -> None:
        self.calls: list = []
        self._outcome = outcome

    def __call__(self, row) -> str:
        self.calls.append(row)
        return self._outcome


class _DrainHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_path = self.dir / "supervisor-lease.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.lease_path)
        self.drain_sender = _RecordingSender()
        self.ws = SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "repoA"))
        self.source_calls = 0

    def _source_fn(self, ws):
        self.source_calls += 1
        raise AssertionError("drain touched the provider")

    def _enqueue_coordinator(self, *, journal: str, gen: str, route: str = "coordinator") -> None:
        self.outbox.enqueue(
            CallbackOutboxKey(
                source="redmine", issue=ISSUE, journal=journal,
                normalized_gate="review_request", callback_route=route, workspace_id="wsA",
            ),
            initial_state=CALLBACK_PENDING,
            target_lane="laneA",
            target_receiver="coordinator",
            enqueue_lane_generation=gen,
            now=CLOCK,
        )

    def _supervisor(self, *, holder="superX", current_gen="5", drain_sender="real",
                    release_after=True, reconcile_due_fn=None):
        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=_boom_source,
            sender_fn=lambda ws: _RecordingSender(),
            clock=lambda: CLOCK,
            release_after=release_after,
            lane_generation_fn=lambda wsid, issue: current_gen,
            drain_sender_fn=(
                (lambda ws: self.drain_sender) if drain_sender == "real" else None
            ),
            reconcile_due_fn=reconcile_due_fn,
        )


class LocalDrainZeroProviderTest(_DrainHarness):
    # -- close condition 1: empty + safe-pending -> 0 provider calls -------

    def test_empty_drain_pass_makes_zero_provider_calls(self) -> None:
        report = self._supervisor().run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(report.mode, SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(report.provider_calls, 0)
        self.assertEqual(report.delivered, 0)
        self.assertTrue(report.empty_pass)
        self.assertEqual(self.drain_sender.calls, [])

    def test_safe_pending_drain_delivers_with_zero_provider_calls(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")  # matches current_gen
        report = self._supervisor(current_gen="5").run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(report.provider_calls, 0)  # never read the provider
        self.assertEqual(report.delivered, 1)  # yet the safe row was delivered
        self.assertEqual(len(self.drain_sender.calls), 1)
        delivered = self.outbox.read(states=[CALLBACK_DELIVERED])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].workspace_id, "wsA")

    def test_local_drain_shares_the_pass_external_mutation_budget(self) -> None:
        # Review j#87204 R3-F1: the LOCAL_DRAIN sweep shares the pass's ONE external-mutation budget
        # (Final Disposition j#87188 = B). Two workspaces each with a locally-attestable coordinator
        # row -> only the FIRST (deterministic workspace-id order) delivers; the second is
        # budget-deferred with its row left PENDING, delivered on the NEXT pass with NO duplicate.
        for wsid, issue, journal in (("wsA", "14219", "100"), ("wsB", "14220", "200")):
            self.outbox.enqueue(
                CallbackOutboxKey(
                    source="redmine", issue=issue, journal=journal,
                    normalized_gate="review_request", callback_route="coordinator", workspace_id=wsid,
                ),
                initial_state=CALLBACK_PENDING, target_lane="lane_" + wsid,
                target_receiver="coordinator", enqueue_lane_generation="5", now=CLOCK,
            )
        calls: list = []

        def _sender(row):
            calls.append(row.workspace_id)
            return SEND_DELIVERED

        sup = WorkspaceCallbackSupervisor(
            holder="superX", lease_store=self.lease_store, store=self.store, outbox=self.outbox,
            workspaces_fn=lambda: [
                SupervisedWorkspace(workspace_id="wsB", canonical_path=str(self.dir / "b")),
                SupervisedWorkspace(workspace_id="wsA", canonical_path=str(self.dir / "a")),
            ],
            roster_fn=lambda ws: ((), ""), redmine_source_fn=_boom_source,
            sender_fn=lambda ws: _RecordingSender(), clock=lambda: CLOCK,
            lane_generation_fn=lambda wsid, issue: "5", drain_sender_fn=lambda ws: _sender,
        )
        p1 = sup.run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(calls, ["wsA"])          # ONE external send for the whole pass (id order)
        self.assertEqual(p1.delivered, 1)
        by = {w.workspace_id: w for w in p1.workspaces}
        self.assertEqual(by["wsB"].skipped_reason, SKIP_PASS_BUDGET_SPENT)  # deferred, not delivered
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)  # wsB row still pending
        sup.run_once(mode=SUPERVISION_LOCAL_DRAIN)                 # remainder next pass
        self.assertEqual(calls, ["wsA", "wsB"])   # wsB now delivered; wsA NOT re-sent (no duplicate)

    # -- close condition: defer, never blind-send --------------------------

    def test_mismatched_generation_is_deferred_not_sent(self) -> None:
        self._enqueue_coordinator(journal="100", gen="4")  # stale: lane advanced to 5
        report = self._supervisor(current_gen="5").run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(report.provider_calls, 0)
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.deferred, 1)
        self.assertEqual(self.drain_sender.calls, [])  # never blind-sent
        # Deferred, NOT terminal: the row stays pending for the provider reconciliation leg.
        pending = self.outbox.read(states=[CALLBACK_PENDING])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, 0)  # a defer never consumes a retry

    def test_unresolvable_local_generation_defers_the_issue(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")
        report = self._supervisor(current_gen="").run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.deferred, 1)
        self.assertEqual(self.drain_sender.calls, [])
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)

    def test_non_coordinator_route_is_never_claimed_by_the_drain(self) -> None:
        self._enqueue_coordinator(journal="200", gen="5", route="review_return:laneA")
        report = self._supervisor(current_gen="5").run_once(mode=SUPERVISION_LOCAL_DRAIN)
        # No coordinator drain issue -> nothing supervised, nothing delivered, provider untouched.
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.provider_calls, 0)
        self.assertEqual(self.drain_sender.calls, [])
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)  # left for reconcile

    def test_no_drain_sender_defers_everything(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")
        report = self._supervisor(current_gen="5", drain_sender="none").run_once(
            mode=SUPERVISION_LOCAL_DRAIN
        )
        self.assertEqual(report.delivered, 0)
        self.assertEqual(report.deferred, 1)
        self.assertEqual(len(self.outbox.read(states=[CALLBACK_PENDING])), 1)

    # -- close condition 4: duplicate-owner fence preserved ----------------

    def test_duplicate_owner_fences_the_drain(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")
        self.lease_store.acquire("wsA", "otherSuper", now=CLOCK, ttl_seconds=600)
        report = self._supervisor(holder="superX", current_gen="5").run_once(
            mode=SUPERVISION_LOCAL_DRAIN
        )
        ws = report.workspaces[0]
        self.assertFalse(ws.lease_acquired)
        self.assertEqual(ws.skipped_reason, SKIP_LEASE_REFUSED)
        self.assertEqual(self.drain_sender.calls, [])  # zero delivery under a live duplicate owner


class LeaseLifecycleTest(_DrainHarness):
    """The j#83437 / j#83443 lease-retention evidence, deterministically fixed (#14150)."""

    def test_release_all_leases_frees_a_terminated_holders_leases(self) -> None:
        # A bounded watch held the lease (release_after=False) and then terminated.
        held = self._supervisor(holder="watchHolder", release_after=False)
        self.lease_store.acquire("wsA", "watchHolder", now=CLOCK, ttl_seconds=600)
        self.assertIsNotNone(self.lease_store.holder_of("wsA"))
        released = held.release_all_leases()
        self.assertEqual(released, ("wsA",))
        self.assertIsNone(self.lease_store.holder_of("wsA"))  # no starvation until TTL

    def test_a_fresh_run_after_release_is_not_starved(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")
        self.lease_store.acquire("wsA", "watchHolder", now=CLOCK, ttl_seconds=600)
        self._supervisor(holder="watchHolder", release_after=False).release_all_leases()
        # A fresh holder now drains without starving on the terminated holder's stale lease.
        report = self._supervisor(holder="freshHolder", current_gen="5").run_once(
            mode=SUPERVISION_LOCAL_DRAIN
        )
        self.assertTrue(report.workspaces[0].lease_acquired)
        self.assertEqual(report.delivered, 1)

    def test_release_all_leases_never_evicts_an_active_duplicate_owner(self) -> None:
        # A NEW live owner holds the lease; the terminated holder's release must NOT evict it.
        self.lease_store.acquire("wsA", "liveOwner", now=CLOCK, ttl_seconds=600)
        released = self._supervisor(holder="terminatedHolder").release_all_leases()
        self.assertEqual(released, ())  # token-conditional: nothing of ours to release
        lease = self.lease_store.holder_of("wsA")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.holder, "liveOwner")  # the live owner is untouched


class ReconcileCadenceDowngradeTest(_DrainHarness):
    """Redmine #14150: a workspace NOT due for a provider reconcile downgrades to a local drain."""

    def test_not_due_reconcile_downgrades_to_local_drain(self) -> None:
        self._enqueue_coordinator(journal="100", gen="5")
        sup = self._supervisor(current_gen="5", reconcile_due_fn=lambda wsid: False)
        # bounded_reconciliation mode, but not due -> local drain (no provider, _boom_source untouched).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
            SUPERVISION_BOUNDED_RECONCILIATION,
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(report.provider_calls, 0)  # downgraded: zero provider reads
        self.assertEqual(report.delivered, 1)  # the safe row still delivered locally
        self.assertEqual(len(self.drain_sender.calls), 1)


class ReconcileCadenceStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = ReconcileCadenceStore(path=self.dir / "reconcile-cadence.sqlite")

    def test_missing_watermark_reads_as_never_reconciled(self) -> None:
        wm = self.store.read("wsA")
        self.assertEqual(wm.last_reconciled_at, "")  # never -> due
        self.assertEqual(wm.empty_passes, 0)

    def test_empty_passes_accumulate_and_reset_on_produced(self) -> None:
        self.store.mark("wsA", now="2026-07-20T00:00:00+00:00", produced=False)
        self.store.mark("wsA", now="2026-07-20T00:05:00+00:00", produced=False)
        self.assertEqual(self.store.read("wsA").empty_passes, 2)
        self.store.mark("wsA", now="2026-07-20T00:10:00+00:00", produced=True)
        wm = self.store.read("wsA")
        self.assertEqual(wm.empty_passes, 0)  # a produced pass resets the backoff
        self.assertEqual(wm.last_reconciled_at, "2026-07-20T00:10:00+00:00")


class ProviderCallCountTest(unittest.TestCase):
    """Redmine #14150 review F2: provider_calls is the ACTUAL read_entries count, not issues-touched."""

    def test_provider_calls_counts_actual_reads_not_issues(self) -> None:
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
            MappingRedmineJournalSource,
        )

        d = Path(tempfile.mkdtemp())
        sp = d / "wf.sqlite"
        payload = {
            "issue": {"id": ISSUE},
            "journals": [
                {"id": "100", "notes": "## Gate: review_request\n"
                 "[mozyo:workflow-event:gate=review_request:conclusion=pending]"}
            ],
        }
        src = MappingRedmineJournalSource(payload=payload)
        sup = WorkspaceCallbackSupervisor(
            holder="h",
            lease_store=SupervisorLeaseStore(path=d / "l.sqlite"),
            store=WorkflowRuntimeStore(path=sp),
            outbox=CallbackOutbox(path=sp),
            workspaces_fn=lambda: [SupervisedWorkspace(workspace_id="wsA", canonical_path=str(d))],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=lambda ws: src,
            sender_fn=lambda ws: _RecordingSender(),
            clock=lambda: CLOCK,
        )
        report = sup.run_once()
        # One supervised issue, but it fires supply + discovery + ... reads -> provider_calls > 1.
        self.assertGreaterEqual(report.provider_calls, 2)
        self.assertEqual(report.workspaces[0].provider_read_issues, 1)  # one issue touched provider


class ProviderCallCountRoundFenceTest(unittest.TestCase):
    """Redmine #14150 review F1: provider_calls counts the send-edge round-fence reads too, via a
    shared per-workspace counter — not just the reconcile source's reads."""

    def test_shared_counter_folds_send_edge_reads(self) -> None:
        from mozyo_bridge.core.state.callback_outbox import CallbackOutboxKey
        from mozyo_bridge.core.state.workflow_runtime_store import (
            CALLBACK_PENDING as _CP,
            WorkflowRuntimeStore,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_wiring import (
            _CountingSource,
            _ProviderCallCounter,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
            MappingRedmineJournalSource,
        )

        d = Path(tempfile.mkdtemp())
        sp = d / "wf.sqlite"
        # A pending coordinator row so a delivery (and the sender) actually runs.
        CallbackOutbox(path=sp).enqueue(
            CallbackOutboxKey(source="redmine", issue=ISSUE, journal="100",
                              normalized_gate="review_request", callback_route="coordinator",
                              workspace_id="wsA"),
            initial_state=_CP, target_receiver="coordinator", target_lane="laneA",
        )
        payload = {"issue": {"id": ISSUE}, "journals": [
            {"id": "100", "notes": "## Gate: review_request\n"
             "[mozyo:workflow-event:gate=review_request:conclusion=pending]"}]}
        reconcile_src = MappingRedmineJournalSource(payload=payload)
        shared = _ProviderCallCounter()
        round_fence_reads = {"n": 0}

        def sender_reading_provider(row):
            # Simulate the send-edge round fence: read the provider through a source that SHARES the
            # workspace counter (as build_supervisor wires it), then deliver.
            _CountingSource(reconcile_src, shared).read_entries(ISSUE)
            round_fence_reads["n"] += 1
            return SEND_DELIVERED

        sup = WorkspaceCallbackSupervisor(
            holder="h",
            lease_store=SupervisorLeaseStore(path=d / "l.sqlite"),
            store=WorkflowRuntimeStore(path=sp),
            outbox=CallbackOutbox(path=sp),
            workspaces_fn=lambda: [SupervisedWorkspace(workspace_id="wsA", canonical_path=str(d))],
            roster_fn=lambda ws: ((ISSUE,), ""),
            redmine_source_fn=lambda ws: reconcile_src,
            sender_fn=lambda ws: sender_reading_provider,
            clock=lambda: CLOCK,
            provider_counter_fn=lambda wsid: shared,  # the shared per-ws counter (build_supervisor wiring)
        )
        report = sup.run_once()
        reconcile_only = report.provider_calls - round_fence_reads["n"]
        self.assertGreaterEqual(round_fence_reads["n"], 1)  # the sender read the provider
        self.assertGreaterEqual(reconcile_only, 1)  # the reconcile source read the provider
        # provider_calls folds BOTH the reconcile reads AND the send-edge round-fence reads.
        self.assertEqual(report.provider_calls, reconcile_only + round_fence_reads["n"])


class SchemaForwardCompatTest(unittest.TestCase):
    """Redmine #14150: a strict read-only read must tolerate a store predating the additive column.

    ``read_strict_readonly`` never migrates (its contract is 'verdict only; writes nothing' — the
    retire obligation gate). A store created by an earlier build (v3, before ``enqueue_lane_generation``)
    must still read as a valid subset, or the obligation gate fail-closes to ``obligation_unreadable``.
    """

    def test_strict_read_tolerates_a_pre_column_store(self) -> None:
        import sqlite3

        d = Path(tempfile.mkdtemp())
        p = d / "workflow-runtime.sqlite"
        conn = sqlite3.connect(p)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "CREATE TABLE callback_outbox (source TEXT NOT NULL, issue TEXT NOT NULL, "
            "journal TEXT NOT NULL, normalized_gate TEXT NOT NULL, callback_route TEXT NOT NULL, "
            "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL "
            "DEFAULT 3, send_attempted INTEGER NOT NULL DEFAULT 0, claim_token TEXT NOT NULL DEFAULT '', "
            "claimed_at TEXT NOT NULL DEFAULT '', notification_kind TEXT NOT NULL DEFAULT '', "
            "notification_summary TEXT NOT NULL DEFAULT '', gate_mismatch INTEGER NOT NULL DEFAULT 0, "
            "detail TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '', workspace_id TEXT NOT "
            "NULL DEFAULT '', target_lane TEXT NOT NULL DEFAULT '', target_receiver TEXT NOT NULL "
            "DEFAULT '', target_generation TEXT NOT NULL DEFAULT '', seq INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "UNIQUE(workspace_id, source, issue, journal, normalized_gate, callback_route))"
        )
        conn.execute(
            "INSERT INTO callback_outbox VALUES ('redmine','1','2','g','coordinator','pending',0,3,0,"
            "'','','','',0,'','','w','','','',0,'t','t')"
        )
        conn.commit()
        conn.close()
        outbox = CallbackOutbox(path=p)
        rows = outbox.read_strict_readonly(states=[CALLBACK_PENDING])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].enqueue_lane_generation, "")  # absent column -> default, no raise


class ReconcileIncrementalDomainTest(unittest.TestCase):
    """Redmine #14150 review F2: pure changed-work reconcile selection."""

    def _select(self, **kw):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
            select_reconcile_issues,
        )
        base = dict(
            changed_ids=frozenset(), changed_ok=True,
            snapshot_by_issue={}, prior_snapshot_by_issue={}, has_local_work=frozenset(),
        )
        base.update(kw)
        return select_reconcile_issues(["1", "2", "3"], **base)

    def test_changed_external_is_reconciled(self) -> None:
        to, skip = self._select(changed_ids=frozenset({"2"}))
        self.assertEqual(to, ("2",))
        self.assertEqual(skip, ("1", "3"))

    def test_local_snapshot_change_forces_refetch_even_if_not_externally_changed(self) -> None:
        # F3-safety: issue 2's local snapshot moved (owner resolved) but Redmine did NOT change it.
        to, skip = self._select(
            changed_ids=frozenset(),
            snapshot_by_issue={"1": "a", "2": "NEW", "3": "c"},
            prior_snapshot_by_issue={"1": "a", "2": "OLD", "3": "c"},
        )
        self.assertIn("2", to)  # re-fetched despite no external change -> a cleared refusal is re-seen
        self.assertNotIn("2", skip)

    def test_un_accounted_local_work_is_reconciled(self) -> None:
        to, _ = self._select(has_local_work=frozenset({"3"}))
        self.assertIn("3", to)

    def test_failed_changed_query_fails_open_reconciles_all(self) -> None:
        to, skip = self._select(changed_ok=False, changed_ids=frozenset())
        self.assertEqual(set(to), {"1", "2", "3"})  # never suppress the provider fallback
        self.assertEqual(skip, ())


class ReconcileChangedWorkStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReconcileCadenceStore(path=Path(tempfile.mkdtemp()) / "rc.sqlite")

    def test_issue_snapshot_roundtrip(self) -> None:
        self.assertEqual(self.store.read_issue_snapshots("wsA", ["1"]), {"1": ""})
        self.store.write_issue_snapshot("wsA", "1", snapshot="snap-1", now=CLOCK)
        self.assertEqual(self.store.read_issue_snapshots("wsA", ["1", "2"]), {"1": "snap-1", "2": ""})

    def test_changed_watermark_roundtrip(self) -> None:
        self.assertEqual(self.store.read_changed_watermark("wsA"), "")
        self.store.advance_changed_watermark("wsA", watermark="2026-07-20T00:00:00+00:00", now=CLOCK)
        self.assertEqual(self.store.read_changed_watermark("wsA"), "2026-07-20T00:00:00+00:00")


class ReconcileIncrementalWiringTest(unittest.TestCase):
    """Redmine #14150 review F2: the built selector folds changed-work + local snapshot + work."""

    def _build(self, *, changed, changed_ok, lane_facts, prior_snapshots=None, local_work=()):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_changed_work import (
            build_reconcile_incremental_fn,
        )
        d = Path(tempfile.mkdtemp())
        store = ReconcileCadenceStore(path=d / "rc.sqlite")
        for iss, snap in (prior_snapshots or {}).items():
            store.write_issue_snapshot("wsA", iss, snapshot=snap, now=CLOCK)
        outbox = CallbackOutbox(path=d / "wf.sqlite")
        for iss in local_work:
            outbox.enqueue(
                CallbackOutboxKey(source="redmine", issue=iss, journal="9", normalized_gate="g",
                                  callback_route="coordinator", workspace_id="wsA"),
                initial_state=CALLBACK_PENDING,
            )
        fn = build_reconcile_incremental_fn(
            cadence_store=store, lifecycle_store=object(), outbox=outbox,
            lane_facts_fn=lambda ws, i: lane_facts.get(i, ("", 0, "")),
            authoritative_map_fn=lambda: {},
            changed_work_fn=lambda ids, since: (frozenset(changed), "2026-07-20T01:00:00+00:00", changed_ok),
            now_fn=lambda: CLOCK,
        )
        return fn, store

    def test_snapshot_change_forces_refetch(self) -> None:
        # Prior snapshot for issue 2 was under a stale (ambiguous) owner; the live lane_facts now differ
        # (resolved) -> issue 2 is reconciled even though the changed-work query returned nothing.
        fn, _ = self._build(
            changed=set(), changed_ok=True,
            lane_facts={"1": ("l1", 1, "active"), "2": ("l2", 2, "active")},
            prior_snapshots={"1": issue_reconcile_snapshot_str("l1", 1, "active", ""),
                             "2": issue_reconcile_snapshot_str("l2", 1, "active", "")},  # gen 1 != live 2
        )
        to, skip, _commit = fn("wsA", ["1", "2"])
        self.assertIn("2", to)   # snapshot moved -> re-fetch
        self.assertIn("1", skip)  # unchanged -> skip

    def test_commit_advances_watermark_and_snapshot(self) -> None:
        fn, store = self._build(changed={"1"}, changed_ok=True, lane_facts={"1": ("l1", 1, "active")})
        to, _skip, commit = fn("wsA", ["1"])
        self.assertEqual(to, ("1",))
        commit(["1"])  # a successful reconcile
        self.assertEqual(store.read_changed_watermark("wsA"), "2026-07-20T01:00:00+00:00")
        self.assertNotEqual(store.read_issue_snapshots("wsA", ["1"])["1"], "")


class ReconcileIncrementalRecoveryTest(unittest.TestCase):
    """Redmine #14150 R3 review j#84332: bootstrap seed (F1), partial-failure watermark (F2),
    unreadable-outbox fail-open (F3)."""

    def _fn(self, *, changed_work_fn, outbox=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_changed_work import (
            build_reconcile_incremental_fn,
        )
        d = Path(tempfile.mkdtemp())
        store = ReconcileCadenceStore(path=d / "rc.sqlite")
        ob = outbox if outbox is not None else CallbackOutbox(path=d / "wf.sqlite")
        fn = build_reconcile_incremental_fn(
            cadence_store=store, lifecycle_store=object(), outbox=ob,
            lane_facts_fn=lambda ws, i: ("lane", 1, "active"),
            authoritative_map_fn=lambda: {},
            changed_work_fn=changed_work_fn, now_fn=lambda: CLOCK,
        )
        return fn, store

    def test_f1_bootstrap_seeds_watermark_then_goes_incremental(self) -> None:
        calls = []

        def cw(ids, since):
            calls.append(since)
            if not since:  # bootstrap: all changed + seed watermark
                return frozenset(ids), "2026-07-20T10:00:00+00:00", True
            return frozenset(), "2026-07-20T11:00:00+00:00", True  # 2nd pass: nothing changed

        fn, store = self._fn(changed_work_fn=cw)
        to1, skip1, commit1 = fn("wsA", ["1", "2"])
        self.assertEqual(set(to1), {"1", "2"})  # bootstrap reconciles all
        commit1(["1", "2"])  # full success
        self.assertEqual(store.read_changed_watermark("wsA"), "2026-07-20T10:00:00+00:00")  # SEEDED
        # Second pass: watermark is set -> query runs incrementally, snapshots now match -> skip.
        to2, skip2, _ = fn("wsA", ["1", "2"])
        self.assertEqual(calls[1], "2026-07-20T10:00:00+00:00")  # queried WITH the seeded since
        self.assertEqual(to2, ())          # nothing changed -> incremental skip
        self.assertEqual(set(skip2), {"1", "2"})

    def test_f2_partial_failure_does_not_advance_watermark(self) -> None:
        fn, store = self._fn(
            changed_work_fn=lambda ids, since: (frozenset({"1", "2"}), "2026-07-20T12:00:00+00:00", True)
        )
        store.advance_changed_watermark("wsA", watermark="2026-07-20T00:00:00+00:00", now=CLOCK)
        to, _skip, commit = fn("wsA", ["1", "2"])
        self.assertEqual(set(to), {"1", "2"})
        commit(["1"])  # issue 2 FAILED (not in reconciled set)
        # Watermark unchanged -> next pass re-queries from the old window (issue 2 is re-found).
        self.assertEqual(store.read_changed_watermark("wsA"), "2026-07-20T00:00:00+00:00")

    def test_f3_unreadable_outbox_fails_open_reconciles_all(self) -> None:
        class _BoomOutbox:
            def read(self, states=None):
                raise RuntimeError("outbox unreadable")

        # No provider change AND snapshots would match, but the unreadable outbox forces a full reconcile.
        fn, store = self._fn(
            changed_work_fn=lambda ids, since: (frozenset(), "2026-07-20T12:00:00+00:00", True),
            outbox=_BoomOutbox(),
        )
        # Seed snapshots so they'd otherwise match (no snapshot-change).
        store.write_issue_snapshot("wsA", "1", snapshot=issue_reconcile_snapshot_str("lane", 1, "active", ""), now=CLOCK)
        to, skip, _ = fn("wsA", ["1"])
        self.assertEqual(set(to), {"1"})  # fail-open: reconciled despite no change (outbox unverifiable)
        self.assertEqual(skip, ())


class RedmineChangedWorkAdapterTest(unittest.TestCase):
    """Redmine #14150 R3 review j#84332 verification gap: direct adapter query/credential/parse contract."""

    def _fake_urlopen(self, body):
        import contextlib

        @contextlib.contextmanager
        def _open(request, timeout=None):
            self._last_request = request
            yield type("R", (), {"read": lambda _self: body.encode("utf-8")})()
        return _open

    def test_bootstrap_blank_since_returns_all_and_seeds_no_query(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_changed_work import (
            redmine_changed_issue_ids,
        )
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("bootstrap must not query the provider")

        changed, wm, ok = redmine_changed_issue_ids(["1", "2"], "", now="2026-07-20T10:00:00+00:00", urlopen=_boom)
        self.assertEqual(changed, frozenset({"1", "2"}))
        self.assertEqual(wm, "2026-07-20T10:00:00+00:00")
        self.assertTrue(ok)
        self.assertEqual(called["n"], 0)

    def test_query_parses_changed_ids_and_filters_to_roster(self) -> None:
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            reconcile_changed_work as rcw,
        )
        body = '{"issues": [{"id": 1}, {"id": 99}]}'  # 99 is not in the roster -> filtered out
        creds = type("C", (), {"api_key": "k", "base_url": "https://redmine.test"})()
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials.resolve_redmine_credentials",
            return_value=creds,
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context.normalize_base_url",
            return_value="https://redmine.test",
        ):
            changed, wm, ok = rcw.redmine_changed_issue_ids(
                ["1", "2"], "2026-07-20T00:00:00+00:00", now="2026-07-20T10:00:00+00:00",
                urlopen=self._fake_urlopen(body),
            )
        self.assertTrue(ok)
        self.assertEqual(changed, frozenset({"1"}))  # 1 changed & in roster; 99 filtered
        self.assertEqual(wm, "2026-07-20T10:00:00+00:00")
        self.assertIn("updated_on", self._last_request.full_url)  # the query carried the since filter

    def test_missing_credentials_fail_open(self) -> None:
        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            reconcile_changed_work as rcw,
        )
        creds = type("C", (), {"api_key": None, "base_url": None})()
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials.resolve_redmine_credentials",
            return_value=creds,
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context.normalize_base_url",
            return_value=None,
        ):
            changed, wm, ok = rcw.redmine_changed_issue_ids(
                ["1"], "2026-07-20T00:00:00+00:00", now="2026-07-20T10:00:00+00:00",
                urlopen=self._fake_urlopen("{}"),
            )
        self.assertFalse(ok)  # fail-open -> selector reconciles all
        self.assertEqual(changed, frozenset())


class SupervisorHonorsIncrementalTest(unittest.TestCase):
    """Redmine #14150 review F2: the supervisor provider-reads ONLY the selector's to-reconcile subset."""

    def test_skipped_issue_gets_no_provider_read(self) -> None:
        from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
            SUPERVISION_BOUNDED_RECONCILIATION,
        )

        d = Path(tempfile.mkdtemp())
        sp = d / "wf.sqlite"
        read_issues: list = []

        class _RecordingSource:
            def read_entries(self, issue_id=None):
                read_issues.append(str(issue_id))
                return []

        committed: dict = {}

        def incremental(wsid, roster):
            def commit(reconciled):
                committed["reconciled"] = list(reconciled)
            return ("1",), ("2",), commit  # reconcile issue 1, skip issue 2

        sup = WorkspaceCallbackSupervisor(
            holder="h",
            lease_store=SupervisorLeaseStore(path=d / "l.sqlite"),
            store=WorkflowRuntimeStore(path=sp),
            outbox=CallbackOutbox(path=sp),
            workspaces_fn=lambda ws=None: [SupervisedWorkspace(workspace_id="wsA", canonical_path=str(d))],
            roster_fn=lambda ws: (("1", "2"), ""),
            redmine_source_fn=lambda ws: _RecordingSource(),
            sender_fn=lambda ws: _RecordingSender(),
            clock=lambda: CLOCK,
            reconcile_incremental_fn=incremental,
        )
        report = sup.run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        ws = report.workspaces[0]
        supervised_in_pass = [o.issue for o in ws.issues]
        self.assertEqual(supervised_in_pass, ["1"])         # only issue 1 provider-reconciled
        self.assertEqual(ws.reconcile_skipped_issues, ("2",))  # issue 2 skipped (no provider read)
        self.assertNotIn("2", read_issues)                   # the skipped issue was never read
        self.assertEqual(committed.get("reconciled"), ["1"])  # commit ran for the reconciled issue


def issue_reconcile_snapshot_str(lane_id, generation, disposition, owner):
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
        issue_reconcile_snapshot,
    )
    return issue_reconcile_snapshot(lane_id, generation, disposition, owner)


class PureHelperTest(unittest.TestCase):
    def test_backoff_is_monotonic_and_capped(self) -> None:
        base = reconcile_backoff_seconds(60, 0, max_interval_seconds=600)
        self.assertEqual(base, 60)
        self.assertEqual(reconcile_backoff_seconds(60, 1, max_interval_seconds=600), 120)
        self.assertEqual(reconcile_backoff_seconds(60, 3, max_interval_seconds=600), 480)
        self.assertEqual(reconcile_backoff_seconds(60, 30, max_interval_seconds=600), 600)  # capped
        # A huge empty count never overflows and stays capped.
        self.assertEqual(reconcile_backoff_seconds(60, 10_000, max_interval_seconds=600), 600)

    def test_backoff_jitter_only_adds_within_the_interval(self) -> None:
        low = reconcile_backoff_seconds(60, 2, max_interval_seconds=600, jitter_unit=0.0, jitter_fraction=0.5)
        high = reconcile_backoff_seconds(60, 2, max_interval_seconds=600, jitter_unit=0.99, jitter_fraction=0.5)
        self.assertEqual(low, 240)  # jitter_unit 0 adds nothing
        self.assertGreater(high, low)  # jitter adds
        self.assertLessEqual(high, 600)  # never above the ceiling

    def test_should_reconcile_gate(self) -> None:
        self.assertTrue(should_reconcile_source("", "2026-07-20T00:00:00+00:00", 300))  # never
        self.assertFalse(
            should_reconcile_source("2026-07-20T00:00:00+00:00", "2026-07-20T00:02:00+00:00", 300)
        )
        self.assertTrue(
            should_reconcile_source("2026-07-20T00:00:00+00:00", "2026-07-20T00:10:00+00:00", 300)
        )
        self.assertTrue(should_reconcile_source("2026-07-20T00:00:00+00:00", "not-a-time", 300))

    def test_attestable_route_and_drain_selection(self) -> None:
        self.assertTrue(is_locally_attestable_route("coordinator"))
        self.assertFalse(is_locally_attestable_route("review_return:x"))
        self.assertFalse(is_locally_attestable_route("lane_gateway:x"))
        rows = [
            type("R", (), {"workspace_id": "w", "callback_route": "coordinator", "issue": "1"})(),
            type("R", (), {"workspace_id": "w", "callback_route": "review_return:x", "issue": "2"})(),
            type("R", (), {"workspace_id": "other", "callback_route": "coordinator", "issue": "3"})(),
        ]
        self.assertEqual(select_drain_issues(rows, "w"), ("1",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
