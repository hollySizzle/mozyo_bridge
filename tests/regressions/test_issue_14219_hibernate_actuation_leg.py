"""Auto-hibernate actuation leg, end-to-end (Redmine #14219, tranche T2a).

Drives ``run_hibernate_pass`` against the REAL ``SublaneHibernateUseCase`` over a seeded
``LaneLifecycleStore`` + a fake IO port, proving the actuation-safety invariants:

- **happy path** — one approved candidate hibernates: the store row flips ``active -> hibernated``,
  ``mutations == 1``;
- **at most one mutation per pass** — with two eligible lanes, exactly one is actuated and the other
  is deferred; only one store row changes;
- **lease-gated** — a lost lease before the execute stops the pass with zero mutation (the row stays
  active);
- **pinned CAS** — a candidate whose ``expected_revision`` no longer matches the store (a raced
  generation) is refused: blocked, zero mutation, no retry;
- **blocked preflight → no retry** — an unmet obligation blocks with zero mutation;
- **missing basis journal** — fail-closed for that candidate, the use case is never driven;
- **empty pass** — zero mutation, zero ops interaction.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    SublaneHibernateUseCase,
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary import (  # noqa: E501
    LaneActivityObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    ATTEMPT_ACTUATED,
    ATTEMPT_BLOCKED,
    ATTEMPT_DEFERRED,
    ATTEMPT_LEASE_LOST,
    ATTEMPT_NO_JOURNAL,
    ATTEMPT_STALE,
    run_hibernate_pass,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_source import (  # noqa: E501
    still_current,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_actuation as ha,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_candidate as hc,
)

WS = "ws-auto"
ISSUE = "14219"
LANE = "lane-auto"
JOURNAL = "85508"


class _FakeOps:
    """Minimal SublaneHibernateOps: a clean, quiescent, releasable lane."""

    def __init__(self):
        self.close_calls: list = []
        self.executed_reads = 0

    def workspace_id(self) -> str:
        return WS

    def read_inventory(self):
        return [], True  # no live slots -> release is not_requested -> clean success

    def read_attestation(self, assigned_name):
        return None

    def read_worktree_mutation(self):
        self.executed_reads += 1
        return WorktreeMutationFingerprint(readable=True)

    def read_lane_activity(self, workspace_id, lane, rows):
        return LaneActivityObservation(readable=True)

    def execute_close(self, plan):
        self.close_calls.append(plan)
        return HerdrRetireCloseResult(
            workspace_id=plan.workspace_id, lane_id=plan.lane_id,
            closed=tuple(plan.close_targets), failed=(), foreign_names=plan.foreign_names,
        )


def _decision(journal=JOURNAL, issue=ISSUE) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _candidate(*, lane=LANE, issue=ISSUE, ws=WS, gen=1, rev=1, basis=hc.BASIS_DEPENDENCY_PARK):
    anchor = hc.LifecycleAnchor(
        issue_id=issue, repo_workspace_id=ws, lane_id=lane, lane_generation=gen, revision=rev
    )
    return hc.HibernateCandidate(
        issue_id=issue, anchor=anchor,
        head=hc.BoundField(value="a" * 40, provenance=hc.PROVENANCE_GIT_REMOTE),
        basis=basis, conjuncts=(),
    )


def _obligations(**over):
    base = dict(
        callbacks_drained=True, no_review_pending=True, no_owner_approval_pending=True,
        no_integration_pending=True, no_pending_prompt=True, not_working=True,
        worktree_clean=True, boundary_recorded=False,
    )
    base.update(over)
    return ha.ActionTimeObligations(**base)


class HibernateActuationLegTests(unittest.TestCase):
    def _run(self, store, candidates, *, obligations=None, journal=JOURNAL, lease=True,
             current=True, ops=None):
        ops = ops or _FakeOps()
        use_case = SublaneHibernateUseCase(ops=ops, store=store)
        result = run_hibernate_pass(
            candidates,
            revalidate_fn=lambda c: current,
            obligations_fn=lambda c: obligations or _obligations(),
            journal_fn=lambda c: journal,
            use_case=use_case,
            lease_renew_fn=lambda: lease,
        )
        return result, ops

    def _seed(self, home, lane=LANE, issue=ISSUE, ws=WS):
        store = LaneLifecycleStore(home=home)
        store.declare_active(LaneLifecycleKey(ws, lane), decision=_decision(), issue_id=issue)
        return store

    def _disposition(self, store, lane=LANE, ws=WS):
        return store.get(LaneLifecycleKey(ws, lane)).lane_disposition

    def test_happy_path_hibernates_one_lane(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, _ = self._run(store, [_candidate()])
            self.assertEqual(result.mutations, 1)
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_ACTUATED)
            self.assertEqual(self._disposition(store), DISPOSITION_HIBERNATED)

    def test_at_most_one_mutation_per_pass(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            # Two DIFFERENT issues (an issue owns at most one active lane in a workspace).
            store = self._seed(home, lane="lane-a", issue="14219")
            store.declare_active(
                LaneLifecycleKey(WS, "lane-b"),
                decision=_decision("85509", issue="14200"), issue_id="14200",
            )
            cands = [
                _candidate(lane="lane-b", issue="14200"),
                _candidate(lane="lane-a", issue="14219"),
            ]
            result, _ = self._run(store, cands)
            self.assertEqual(result.mutations, 1)
            kinds = [a.kind for a in result.attempts]
            self.assertEqual(kinds.count(ATTEMPT_ACTUATED), 1)
            self.assertEqual(kinds.count(ATTEMPT_DEFERRED), 1)
            # exactly one row hibernated; the other still active.
            dispositions = [
                self._disposition(store, lane="lane-a"),
                self._disposition(store, lane="lane-b"),
            ]
            self.assertEqual(dispositions.count(DISPOSITION_HIBERNATED), 1)
            self.assertEqual(dispositions.count(DISPOSITION_ACTIVE), 1)
            # deterministic order by (issue, lane): issue 14200 (lane-b) sorts first, so it is the
            # one actuated; lane-a (issue 14219) is deferred.
            self.assertEqual(self._disposition(store, lane="lane-b"), DISPOSITION_HIBERNATED)
            self.assertEqual(self._disposition(store, lane="lane-a"), DISPOSITION_ACTIVE)

    def test_lost_lease_stops_the_pass_with_zero_mutation(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, ops = self._run(store, [_candidate()], lease=False)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_LEASE_LOST)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_a_drifted_anchor_is_not_actuated(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            # Action-time revalidation says the anchor is no longer current (drifted since build):
            # zero mutation, the use case is never driven.
            result, ops = self._run(store, [_candidate()], current=False)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_STALE)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(ops.executed_reads, 0)
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_still_current_detects_a_drifted_revision(self):
        # The real revalidation predicate: a candidate whose revision no longer matches the store
        # is not current; a matching one is.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)  # declared at revision 1
            self.assertTrue(still_current(_candidate(rev=1), home=home))
            self.assertFalse(still_current(_candidate(rev=2), home=home))
            self.assertFalse(still_current(_candidate(lane="lane-other"), home=home))
            self.assertFalse(still_current(_candidate(gen=5), home=home))

    def test_blocked_preflight_does_not_mutate_and_is_not_retried(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            # not_working=False -> lane not idle -> preflight blocks; zero mutation.
            result, ops = self._run(store, [_candidate()], obligations=_obligations(not_working=False))
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_BLOCKED)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_missing_basis_journal_never_drives_the_use_case(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, ops = self._run(store, [_candidate()], journal="")
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_NO_JOURNAL)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(ops.executed_reads, 0)
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_empty_pass_is_zero_mutation_and_zero_ops(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, ops = self._run(store, [])
            self.assertTrue(result.empty_pass)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts, ())
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(ops.executed_reads, 0)


if __name__ == "__main__":
    unittest.main()
