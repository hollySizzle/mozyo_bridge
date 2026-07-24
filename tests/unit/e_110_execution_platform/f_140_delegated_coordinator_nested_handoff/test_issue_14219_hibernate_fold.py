"""The auto-hibernate leg folded into the bounded pass (Redmine #14219 T3, Answer j#87108; review j#87154 R1).

The T3 ruling (Design Consultation Answer j#87108) folds the hibernate leg into the SAME
``run_once`` bounded pass as ONE leg AFTER the callback/outbox delivery + reconcile legs, reachable
from BOTH ``local_wake`` (event-wake primary) and ``bounded_reconciliation`` (timer/restart
fallback), sharing the pass's one lifecycle-mutation budget and the existing delivery priority.
``local_drain`` never folds it; the standalone ``SUPERVISION_HIBERNATE`` mode stays only as a
production-unreachable internal/test seam delegating to the SAME primitive.

Review j#87154 R1 hardening pinned here:
  * R1-F1 — the fold runs under the workspace's ALREADY-HELD lease (no release-then-re-acquire) and
    shares ONE pass-wide external-mutation budget across ALL workspaces: an earlier workspace's
    delivery mutation OR uncertain delivery defers every later workspace's hibernate.
  * R1-F2 — a ``local_wake`` pass hibernates ONLY the lanes of the exact woken issues; a no-binding
    pass actuates nothing. Full candidate selection is the timer / ``bounded_reconciliation`` path.
  * R1-F3 — a RAISED leg is surfaced as ``uncertain`` and is never an empty pass; its disposition +
    the defer reasons are observable in a closed vocabulary.
  * R1-F4 — released capacity counts only fully-released actuations (not the mutation count), and
    the pass duration is named for what it measures, not mislabelled a time-to-drain latency.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernateAttempt,
    HibernatePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    hibernate_supervisor_wiring as wiring,
    workspace_hibernate_leg as fold_mod,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_BUDGET_DEFERRED,
    SKIP_HIBERNATE_DELIVERY_UNCERTAIN,
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_WAKE_UNBOUND,
    SKIP_LEASE_LOST,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_DRAIN,
    SUPERVISION_LOCAL_WAKE,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
)

CLOCK = "2026-07-24T00:00:00+00:00"


def _boom_source(ws):  # pragma: no cover - the hibernate fold never resolves a provider source
    raise AssertionError("the folded hibernate leg must not resolve a ticket-provider source")


def _mutating_leg(calls, *, mutations=1, kind="actuated", reason="", raises=False):
    """A fake leg WITHOUT a ``restrict_issues`` param (so budget tests exercise the plain path)."""

    def leg(ws, renew, budget=None):
        calls.append(ws.workspace_id)
        if raises:
            raise RuntimeError("leg exploded")
        attempts = (
            (HibernateAttempt(issue="1", lane="l", kind=kind, reason=reason, revision=2),)
            if (mutations or kind)
            else ()
        )
        return HibernatePassResult(attempts=attempts, mutations=mutations, empty_pass=not attempts)

    return leg


def _recording_leg(records, *, mutations=0, kind="deferred"):
    """A fake leg WITH a ``restrict_issues`` param that records the exact scope it was handed."""

    def leg(ws, renew, budget=None, restrict_issues=None):
        records.append((ws.workspace_id, restrict_issues))
        attempts = (HibernateAttempt(issue="1", lane="l", kind=kind, reason="", revision=0),)
        return HibernatePassResult(attempts=attempts, mutations=mutations, empty_pass=False)

    return leg


class _FoldHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=store_path)
        self.outbox = CallbackOutbox(path=store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")

    def _ws(self, wsid):
        return SupervisedWorkspace(workspace_id=wsid, canonical_path=str(self.dir / wsid))

    def _supervisor(self, *, leg=None, workspaces, roster_fn=None, holder="superX",
                    release_after=True):
        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: workspaces,
            roster_fn=roster_fn or (lambda ws: ((), "")),  # empty roster -> no active issues
            redmine_source_fn=_boom_source,
            sender_fn=lambda ws: (lambda row: "delivered"),
            clock=lambda: CLOCK,
            release_after=release_after,
            hibernate_leg_fn=leg,
        )

    def _base(self, wsid, **kw):
        base = dict(workspace_id=wsid, lease_acquired=True, lease_reason="granted_fresh")
        base.update(kw)
        return WorkspaceSupervisionOutcome(**base)


class FoldedRunOnceTest(_FoldHarness):
    def test_bounded_reconciliation_folds_the_hibernate_leg(self) -> None:
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        self.assertEqual(calls, ["wsA"])  # the leg ran as the after-stage of the reconcile pass
        outcome = report.workspaces[0]
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 1)
        self.assertEqual(report.hibernate_applied, 1)

    def test_local_wake_folds_only_when_wake_bound(self) -> None:
        # R1-F2: a local_wake pass hibernates ONLY when a wake names the workspace's issue.
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]
        ).run_once(mode=SUPERVISION_LOCAL_WAKE, wake_hints=[("wsA", "1")])
        self.assertEqual(calls, ["wsA"])
        self.assertTrue(report.workspaces[0].hibernate_ran)

    def test_local_wake_without_a_wake_binding_never_folds(self) -> None:
        # R1-F2 negative: no wake hint -> the leg NEVER runs (only the timer fallback full-scans).
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]
        ).run_once(mode=SUPERVISION_LOCAL_WAKE)  # no wake_hints
        self.assertEqual(calls, [])
        self.assertEqual(report.workspaces[0].hibernate_disposition, SKIP_HIBERNATE_WAKE_UNBOUND)
        self.assertFalse(report.workspaces[0].hibernate_ran)

    def test_local_wake_hibernates_only_the_woken_workspace(self) -> None:
        # R1-F2 negative: a wake for wsA never hibernates the unrelated (unwoken) wsB.
        records: list = []
        self._supervisor(
            leg=_recording_leg(records), workspaces=[self._ws("wsA"), self._ws("wsB")]
        ).run_once(mode=SUPERVISION_LOCAL_WAKE, wake_hints=[("wsA", "1")])
        self.assertEqual([wsid for wsid, _ in records], ["wsA"])  # wsB never actuated

    def test_local_drain_never_folds_the_hibernate_leg(self) -> None:
        calls: list = []
        self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")]).run_once(
            mode=SUPERVISION_LOCAL_DRAIN
        )
        self.assertEqual(calls, [])  # #14150 provider-free drain never mixes hibernate

    def test_the_fold_shares_one_mutation_budget_across_workspaces(self) -> None:
        calls: list = []
        report = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsB"), self._ws("wsA")]
        ).run_once(mode=SUPERVISION_BOUNDED_RECONCILIATION)
        # Deterministic order (wsA before wsB): the first mutates, the second is budget-deferred
        # WITHOUT its leg ever running (zero extra actuation).
        self.assertEqual(calls, ["wsA"])
        self.assertEqual(report.hibernate_applied, 1)
        by_id = {w.workspace_id: w for w in report.workspaces}
        self.assertTrue(by_id["wsA"].hibernate_ran)
        self.assertFalse(by_id["wsB"].hibernate_ran)
        self.assertEqual(by_id["wsB"].hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED)


class HeldLeaseAndPassBudgetTest(_FoldHarness):
    """R1-F1: the fold runs under the HELD lease and shares one pass-wide budget with delivery."""

    def _renew(self):
        return True

    def test_the_fold_never_reacquires_the_lease(self) -> None:
        # The held-lease contract: run_folded_hibernate must NOT acquire a lease (the caller owns it).
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])

        class _PoisonedLease:
            def acquire(self, *a, **k):  # pragma: no cover - asserts it is never called
                raise AssertionError("the folded hibernate leg must NOT re-acquire the lease")

            def renew(self, *a, **k):
                return object()

            def release(self, *a, **k):
                return None

        sup._lease_store = _PoisonedLease()
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        out = fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb,
            bound_issues=(), renew=self._renew,
        )
        self.assertEqual(calls, ["wsA"])  # it ran under the held lease, no re-acquire raised
        self.assertTrue(out.hibernate_ran)

    def test_a_prior_workspace_delivery_defers_the_next_hibernate(self) -> None:
        calls: list = []
        sup = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA"), self._ws("wsB")]
        )
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        # wsA DELIVERED a callback this pass -> its own hibernate defers AND the pass budget is spent.
        out_a = fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA", backlog_delivered=1),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=self._renew,
        )
        fold_mod.mark_pass_budget(pb, out_a)
        self.assertEqual(out_a.hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED)
        self.assertTrue(pb["mutated"])
        # wsB is quiet, but the pass already spent its one external mutation -> deferred, leg unrun.
        out_b = fold_mod.run_folded_hibernate(
            sup, self._ws("wsB"), self._base("wsB"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=self._renew,
        )
        self.assertEqual(calls, [])  # neither leg ran
        self.assertEqual(out_b.hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED)

    def test_a_prior_uncertain_delivery_defers_the_next_hibernate(self) -> None:
        calls: list = []
        sup = self._supervisor(
            leg=_mutating_leg(calls), workspaces=[self._ws("wsA"), self._ws("wsB")]
        )
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        out_a = fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA", skipped_reason=SKIP_LEASE_LOST),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=self._renew,
        )
        fold_mod.mark_pass_budget(pb, out_a)
        self.assertEqual(out_a.hibernate_disposition, SKIP_HIBERNATE_DELIVERY_UNCERTAIN)
        self.assertTrue(pb["uncertain"])
        out_b = fold_mod.run_folded_hibernate(
            sup, self._ws("wsB"), self._base("wsB"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=self._renew,
        )
        self.assertEqual(calls, [])
        self.assertEqual(out_b.hibernate_disposition, SKIP_HIBERNATE_DELIVERY_UNCERTAIN)


class WakeBindingScopeTest(_FoldHarness):
    """R1-F2: the local_wake candidate scope is bound to the woken issues; the leg filters to them."""

    def test_local_wake_binds_the_restrict_scope_to_the_woken_issues(self) -> None:
        records: list = []
        sup = self._supervisor(leg=_recording_leg(records), workspaces=[self._ws("wsA")])
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA"),
            mode=SUPERVISION_LOCAL_WAKE, pass_budget=pb, bound_issues=("7", "9"),
            renew=lambda: True,
        )
        self.assertEqual(records, [("wsA", frozenset({"7", "9"}))])

    def test_bounded_reconciliation_does_a_full_scan_no_restrict(self) -> None:
        records: list = []
        sup = self._supervisor(leg=_recording_leg(records), workspaces=[self._ws("wsA")])
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=("7",),
            renew=lambda: True,
        )
        self.assertEqual(records, [("wsA", None)])  # timer path = whole-workspace scan

    def test_the_production_leg_filters_candidates_to_the_restrict_scope(self) -> None:
        # The leg enumerates a candidate for issue "999"; a restrict scope that excludes it drops it
        # BEFORE assembly. A sentinel stops the probe at the build assembler so the deep actuation
        # phase never runs — the filter is what this test isolates.
        seen: list = []

        class _StopProbe(Exception):
            pass

        class _FakeAssembler:
            def __init__(self, **kw):
                pass

            def assemble_all(self, requests):
                seen.append([str(r.selected.issue_id) for r in requests])
                raise _StopProbe

        class _Sel:
            issue_id = "999"
            repo_workspace_id = "wsA"
            lane_id = "l"
            lane_generation = 1

        class _Req:
            selected = _Sel()

        orig = (wiring.enumerate_requests, wiring.HibernateCandidateAssembler,
                wiring.load_lane_lifecycle_readonly)
        try:
            wiring.enumerate_requests = lambda *a, **k: [_Req()]
            wiring.HibernateCandidateAssembler = _FakeAssembler
            wiring.load_lane_lifecycle_readonly = lambda **k: [object()]
            leg = wiring.build_hibernate_leg_fn(home=None, outbox=self.outbox, source_fn=lambda ws: None)
            ws = self._ws("wsA")
            with self.assertRaises(_StopProbe):
                leg(ws, lambda: True, {"reads": 0}, restrict_issues=frozenset({"1"}))
            self.assertEqual(seen, [[]])  # the unrelated issue was filtered out before assembly
            seen.clear()
            with self.assertRaises(_StopProbe):
                leg(ws, lambda: True, {"reads": 0}, restrict_issues=None)
            self.assertEqual(seen, [["999"]])  # a full (timer) scan keeps it
        finally:
            (wiring.enumerate_requests, wiring.HibernateCandidateAssembler,
             wiring.load_lane_lifecycle_readonly) = orig


class RaisedLegVisibilityTest(_FoldHarness):
    """R1-F3: a RAISED folded leg is UNCERTAIN, never an empty pass, and its disposition is visible."""

    def test_a_raised_leg_surfaces_as_uncertain_not_empty(self) -> None:
        sup = self._supervisor(
            leg=_mutating_leg([], raises=True), workspaces=[self._ws("wsA")]
        )
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        out = fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=lambda: True,
        )
        self.assertEqual(out.hibernate_disposition, SKIP_HIBERNATE_LEG_ERROR)
        report = SupervisorReport(mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h",
                                  workspaces=(out,))
        self.assertEqual(report.hibernate_uncertain, 1)
        self.assertTrue(report.hibernate_ran)
        self.assertFalse(report.empty_pass)  # a fail-closed raised leg is never a healthy empty pass

    def test_a_raised_leg_marks_the_pass_uncertain_and_defers_the_rest(self) -> None:
        sup = self._supervisor(
            leg=_mutating_leg([], raises=True), workspaces=[self._ws("wsA")]
        )
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        out = fold_mod.run_folded_hibernate(
            sup, self._ws("wsA"), self._base("wsA"),
            mode=SUPERVISION_BOUNDED_RECONCILIATION, pass_budget=pb, bound_issues=(),
            renew=lambda: True,
        )
        fold_mod.mark_pass_budget(pb, out)
        self.assertTrue(pb["uncertain"])  # a later workspace defers behind the unknown effect


class ObservabilityMetricsTest(unittest.TestCase):
    """R1-F4 / R2-F2: released capacity is the REAL close count (not the actuated-attempt count);
    claimed is rolled up; the duration is named honestly."""

    def _ws(self, wsid, kind, mutations, released=0):
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid, lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=mutations,
            hibernate_attempts=(
                {"issue": "1", "lane": "l", "kind": kind, "reason": "", "revision": 2,
                 "released": released},
            ),
        )

    def test_released_capacity_is_the_real_close_count(self) -> None:
        # wsA fully released 2 slots; wsB's CAS applied but released 0 (partial / withheld).
        full = self._ws("wsA", "actuated", 1, released=2)
        incomplete = self._ws("wsB", "actuated_release_incomplete", 1, released=0)
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(full, incomplete)
        )
        self.assertEqual(report.hibernate_applied, 2)  # BOTH mutated the lane
        self.assertEqual(report.hibernate_released_capacity, 2)  # only wsA freed slots (2 of them)
        self.assertEqual(report.hibernate_release_incomplete, 1)  # wsB mutated but freed nothing

    def test_not_requested_success_reports_zero_freed_capacity(self) -> None:
        # R2-F2 core: a not_requested release (no live slot) is `actuated` + mutation=1 but freed
        # ZERO slots — released_capacity must NOT count it as freed capacity.
        not_requested = self._ws("wsA", "actuated", 1, released=0)
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(not_requested,)
        )
        self.assertEqual(report.hibernate_applied, 1)  # the lane was hibernated (CAS applied)
        self.assertEqual(report.hibernate_released_capacity, 0)  # but no process slot was freed
        self.assertEqual(report.hibernate_release_incomplete, 1)

    def test_claimed_counts_acted_on_candidates(self) -> None:
        applied = self._ws("wsA", "actuated", 1, released=1)
        blocked = self._ws("wsB", "blocked", 0)
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(applied, blocked)
        )
        self.assertEqual(report.hibernate_claimed, 2)  # applied + blocked were both acted on

    def test_payload_names_the_pass_duration_honestly(self) -> None:
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h",
            workspaces=(self._ws("wsA", "actuated", 1, released=1),), duration_ms=42,
        )
        payload = report.hibernate_payload()
        self.assertEqual(payload["pass_duration_ms"], 42)
        self.assertNotIn("time_to_drain_ms", payload)  # not mislabelled a drain-latency
        self.assertIn("claimed", payload)
        self.assertIn("release_incomplete", payload)


class FoldDeferUnitTest(_FoldHarness):
    """run_folded_hibernate per-workspace defer semantics over crafted base outcomes."""

    def _renew(self):
        return True

    def _fold(self, sup, ws, base, *, mode=SUPERVISION_BOUNDED_RECONCILIATION, bound=()):
        pb = {"reads": 0, "mutated": False, "uncertain": False}
        return fold_mod.run_folded_hibernate(
            sup, ws, base, mode=mode, pass_budget=pb, bound_issues=bound, renew=self._renew
        )

    def test_defers_when_the_workspace_delivered_a_callback(self) -> None:
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        out = self._fold(sup, self._ws("wsA"), self._base("wsA", backlog_delivered=1))
        self.assertEqual(calls, [])
        self.assertFalse(out.hibernate_ran)
        self.assertEqual(out.hibernate_disposition, SKIP_HIBERNATE_BUDGET_DEFERRED)

    def test_defers_on_an_uncertain_delivery(self) -> None:
        for skip in (SKIP_LEASE_LOST, SKIP_ROSTER_UNREADABLE):
            with self.subTest(skip=skip):
                calls: list = []
                sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
                out = self._fold(sup, self._ws("wsA"), self._base("wsA", skipped_reason=skip))
                self.assertEqual(calls, [])
                self.assertEqual(out.hibernate_disposition, SKIP_HIBERNATE_DELIVERY_UNCERTAIN)

    def test_runs_on_a_quiet_lease_held_workspace(self) -> None:
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        out = self._fold(sup, self._ws("wsA"), self._base("wsA"))
        self.assertEqual(calls, ["wsA"])
        self.assertTrue(out.hibernate_ran)

    def test_no_lease_workspace_is_left_untouched(self) -> None:
        calls: list = []
        sup = self._supervisor(leg=_mutating_leg(calls), workspaces=[self._ws("wsA")])
        base = self._base("wsA", lease_acquired=False, skipped_reason="lease_refused_by_other")
        # In run_once, a no-lease workspace never reaches the fold (the wrapper returns before it);
        # exercised here for defensiveness — a lease-refused outcome is only marked, never actuated.
        fold_mod.mark_pass_budget({"reads": 0, "mutated": False, "uncertain": False}, base)
        self.assertEqual(calls, [])


class HibernateObservabilityRollupTest(_FoldHarness):
    def test_report_rolls_up_the_hibernate_metrics_and_empty_pass(self) -> None:
        applied = WorkspaceSupervisionOutcome(
            workspace_id="wsA", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=1,
            hibernate_attempts=(
                {"issue": "1", "lane": "l", "kind": "actuated", "reason": "", "revision": 2,
                 "released": 1},
            ),
        )
        blocked = WorkspaceSupervisionOutcome(
            workspace_id="wsB", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=0,
            hibernate_attempts=(
                {"issue": "2", "lane": "m", "kind": "blocked", "reason": "lane_hibernated",
                 "revision": 0},
            ),
        )
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(applied, blocked)
        )
        self.assertEqual(report.hibernate_applied, 1)
        self.assertEqual(report.hibernate_released_capacity, 1)
        self.assertEqual(report.hibernate_candidates, 2)
        self.assertEqual(report.hibernate_blocked, 1)
        self.assertEqual(report.hibernate_closed_reasons, ("lane_hibernated",))
        self.assertFalse(report.empty_pass)  # a hibernate mutation is NOT an empty pass
        payload = report.as_payload()
        self.assertIn("hibernate", payload)
        self.assertEqual(payload["hibernate"]["applied"], 1)
        self.assertEqual(payload["hibernate"]["released_capacity"], 1)

    def test_a_pass_that_only_evaluated_deferred_candidates_is_not_empty(self) -> None:
        deferred = WorkspaceSupervisionOutcome(
            workspace_id="wsA", lease_acquired=True, lease_reason="g",
            hibernate_ran=True, hibernate_mutations=0,
            hibernate_attempts=(
                {"issue": "1", "lane": "l", "kind": "deferred", "reason": "x", "revision": 0},
            ),
        )
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=(deferred,)
        )
        self.assertFalse(report.empty_pass)  # an attempt was evaluated -> not empty


class NoStandaloneActivationSurfaceTest(unittest.TestCase):
    """Answer j#87108 §3: the CLI / event pump / scheduler adapter must NEVER select the standalone
    SUPERVISION_HIBERNATE mode or expose a public --hibernate action / third scheduler cadence."""

    def _src(self, module):
        return inspect.getsource(module)

    def test_the_cli_never_selects_the_standalone_hibernate_mode(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow_supervisor,
        )

        text = self._src(cli_workflow_supervisor)
        self.assertNotIn("SUPERVISION_HIBERNATE", text)
        self.assertNotIn("--hibernate", text)

    def test_the_event_pump_never_selects_the_standalone_hibernate_mode(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            reconcile_event_pump,
        )

        self.assertNotIn("SUPERVISION_HIBERNATE", self._src(reconcile_event_pump))

    def test_the_launchd_adapter_has_no_third_hibernate_agent(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            supervisor_launchd,
        )

        text = self._src(supervisor_launchd)
        self.assertNotIn("--hibernate", text)
        self.assertNotIn("HIBERNATE_AGENT", text)
        # Owned dual-agent lifecycle preserved (reconcile + drain only).
        self.assertEqual(len(supervisor_launchd.SUPERVISOR_AGENTS), 2)


class HibernateRollupVocabularyDriftGuardTest(unittest.TestCase):
    """The roll-up classifies the leg's attempt-kind tokens + the domain disposition tokens; pin both."""

    def test_the_classified_kinds_are_real_leg_tokens(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            hibernate_actuation_leg as leg,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_report_rollup as roll,
        )

        leg_kinds = {
            getattr(leg, name) for name in dir(leg)
            if name.startswith("ATTEMPT_") and isinstance(getattr(leg, name), str)
        }
        classified = (
            roll._HIBERNATE_APPLIED_KINDS | roll._HIBERNATE_BLOCKED_KINDS
            | roll._HIBERNATE_UNCERTAIN_KINDS | roll._HIBERNATE_DEFERRED_KINDS
        )
        # Every classified token is a real leg attempt-kind (no drift / typo).
        self.assertTrue(classified <= leg_kinds, classified - leg_kinds)

    def test_the_disposition_tokens_match_the_domain_constants(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            hibernate_report_rollup as roll,
            workspace_supervisor as d,
        )

        self.assertEqual(roll._DISPOSITION_LEG_ERROR, d.SKIP_HIBERNATE_LEG_ERROR)
        self.assertEqual(
            roll._DISPOSITION_DEFER_TOKENS,
            frozenset({
                d.SKIP_HIBERNATE_BUDGET_DEFERRED,
                d.SKIP_HIBERNATE_DELIVERY_UNCERTAIN,
                d.SKIP_HIBERNATE_WAKE_UNBOUND,
                d.SKIP_HIBERNATE_UNWIRED,
            }),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
