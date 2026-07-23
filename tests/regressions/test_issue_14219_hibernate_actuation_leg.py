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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    HibernateRequest,
    SublaneHibernateUseCase,
    WorktreeMutationFingerprint,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_assertions import (  # noqa: E501
    HibernateAssertions,
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
    ATTEMPT_PARTIAL,
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
    """Minimal SublaneHibernateOps: a clean, quiescent lane (optionally with live slots)."""

    def __init__(self, rows=None, close_result=None):
        self._rows = list(rows) if rows is not None else []
        self._close_result = close_result
        self.close_calls: list = []
        self.executed_reads = 0

    def workspace_id(self) -> str:
        return WS

    def read_inventory(self):
        # No rows -> release is not_requested -> clean success. Rows + a partial close_result ->
        # RELEASE_PARTIAL (CAS applied, is_success False).
        return list(self._rows), True

    def read_attestation(self, assigned_name):
        return None

    def read_worktree_mutation(self):
        self.executed_reads += 1
        return WorktreeMutationFingerprint(readable=True)

    def read_lane_activity(self, workspace_id, lane, rows):
        return LaneActivityObservation(readable=True)

    def execute_close(self, plan):
        self.close_calls.append(plan)
        if self._close_result is not None:
            return self._close_result
        return HerdrRetireCloseResult(
            workspace_id=plan.workspace_id, lane_id=plan.lane_id,
            closed=tuple(plan.close_targets), failed=(), foreign_names=plan.foreign_names,
        )


def _row(role: str, lane: str) -> dict:
    return {"name": encode_assigned_name(WS, role, lane), "pane_id": f"{WS}:{role}"}


def _request_all_gates(lane=LANE, issue=ISSUE) -> HibernateRequest:
    return HibernateRequest(
        issue=issue, lane=lane, journal=JOURNAL,
        assertions=HibernateAssertions(
            explicitly_parked=True, callbacks_drained=True, no_review_pending=True,
            no_owner_approval_pending=True, no_integration_pending=True, no_pending_prompt=True,
            not_working=True, worktree_clean=True, boundary_recorded=False,
        ),
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
             fresh=True, ops=None, lease_guard=None):
        ops = ops or _FakeOps()
        use_case = SublaneHibernateUseCase(ops=ops, store=store, lease_guard=lease_guard)
        # refresh_fn re-produces the candidate; `fresh=True` -> exact same candidate (current);
        # `fresh=False` -> None (a lapsed basis / drift); or pass a callable for finer control.
        if callable(fresh):
            refresh_fn = fresh
        else:
            refresh_fn = (lambda c: c) if fresh else (lambda c: None)
        result = run_hibernate_pass(
            candidates,
            refresh_fn=refresh_fn,
            obligations_fn=lambda c: obligations or _obligations(),
            journal_fn=lambda c: journal,
            use_case=use_case,
            lease_renew_fn=lambda: lease,
        )
        return result, ops

    def _seed(self, home, lane=LANE, issue=ISSUE, ws=WS):
        store = LaneLifecycleStore(home=home)
        store.declare_active(
            LaneLifecycleKey(ws, lane), decision=_decision(issue=issue), issue_id=issue
        )
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

    def test_a_partial_release_consumes_the_one_mutation_budget(self):
        # R1-F1: the first candidate's CAS applies (row hibernated) but its release is incomplete,
        # so is_success is False. The budget is keyed to transition.applied, so it is consumed:
        # mutations=1, the second candidate is DEFERRED (never a second CAS), and its row stays
        # active. Ordered first is issue 14200/lane-a (the partial one).
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home, lane="lane-a", issue="14200")
            store.declare_active(
                LaneLifecycleKey(WS, "lane-b"),
                decision=_decision("85509", issue="14219"), issue_id="14219",
            )
            partial = HerdrRetireCloseResult(
                workspace_id=WS, lane_id="lane-a",
                closed=(("claude", f"{WS}:claude"),),
                failed=(("codex", f"{WS}:codex", "close_failed"),),
            )
            ops = _FakeOps(
                rows=[_row("codex", "lane-a"), _row("claude", "lane-a")], close_result=partial
            )
            result, _ = self._run(
                store,
                [_candidate(lane="lane-b", issue="14219"), _candidate(lane="lane-a", issue="14200")],
                ops=ops,
            )
            self.assertEqual(result.mutations, 1)
            kinds = [a.kind for a in result.attempts]
            self.assertEqual(kinds.count(ATTEMPT_PARTIAL), 1)
            self.assertEqual(kinds.count(ATTEMPT_DEFERRED), 1)
            self.assertEqual(kinds.count(ATTEMPT_ACTUATED), 0)
            # lane-a hibernated (CAS applied); lane-b never touched.
            self.assertEqual(self._disposition(store, lane="lane-a"), DISPOSITION_HIBERNATED)
            self.assertEqual(self._disposition(store, lane="lane-b"), DISPOSITION_ACTIVE)

    def test_lost_lease_stops_the_pass_with_zero_mutation(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, ops = self._run(store, [_candidate()], lease=False)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_LEASE_LOST)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_a_lapsed_basis_refresh_none_is_not_actuated(self):
        # R1-F3: the composite refresh re-produces nothing (a durable basis lapsed since build):
        # zero mutation, the use case is never driven.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            result, ops = self._run(store, [_candidate()], fresh=False)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_STALE)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(ops.executed_reads, 0)
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_a_non_equal_refresh_is_not_actuated(self):
        # R1-F3: the fresh candidate differs from the built one (e.g. the review head moved):
        # exact-equality fails -> stale zero-actuation, even though a candidate exists.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            moved = _candidate()
            moved = hc.HibernateCandidate(
                issue_id=moved.issue_id, anchor=moved.anchor,
                head=hc.BoundField(value="b" * 40, provenance=hc.PROVENANCE_GIT_REMOTE),
                basis=moved.basis, conjuncts=moved.conjuncts,
            )
            result, ops = self._run(store, [_candidate()], fresh=lambda c: moved)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_STALE)
            self.assertEqual(ops.executed_reads, 0)
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_still_current_detects_a_drifted_lifecycle_row(self):
        # The lifecycle component of the composite: a candidate whose revision/lane/generation no
        # longer matches the store is not current; a matching one is.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)  # declared at revision 1
            self.assertTrue(still_current(_candidate(rev=1), home=home))
            self.assertFalse(still_current(_candidate(rev=2), home=home))
            self.assertFalse(still_current(_candidate(lane="lane-other"), home=home))
            self.assertFalse(still_current(_candidate(gen=5), home=home))

    def test_lease_lost_at_the_commit_boundary_commits_nothing(self):
        # R1-F2: the use case's own lease_guard refuses at the commit boundary (after the wrapper
        # renew passed): zero transition, zero close, the pass stops.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            # wrapper lease renew passes (lease=True), but the commit-point guard refuses.
            result, ops = self._run(store, [_candidate()], lease=True, lease_guard=lambda: False)
            self.assertEqual(result.mutations, 0)
            self.assertEqual(result.attempts[0].kind, ATTEMPT_LEASE_LOST)
            self.assertEqual(ops.close_calls, [])
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

    def test_redrive_close_fence_catches_a_takeover_during_the_boundary_read(self):
        # R2-F1 ordered race: on an already-hibernated redrive, the lease is held at the early
        # check (call 1 -> True), then taken over DURING the boundary read (call 2 -> False),
        # immediately before the close. The commit-point fence must close NOTHING.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
            SublaneHibernateUseCase as UC,
        )

        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            # First: hibernate to a PARTIAL release (row hibernated, a close still owed).
            partial = HerdrRetireCloseResult(
                workspace_id=WS, lane_id=LANE,
                closed=(("claude", f"{WS}:claude"),),
                failed=(("codex", f"{WS}:codex", "close_failed"),),
            )
            ops1 = _FakeOps(rows=[_row("codex", LANE), _row("claude", LANE)], close_result=partial)
            first = UC(ops=ops1, store=store).run(_request_all_gates(), execute=True)
            self.assertTrue(first.transition.applied)
            self.assertEqual(self._disposition(store), DISPOSITION_HIBERNATED)

            # Redrive with a guard that is True only on its first call.
            calls = {"n": 0}

            def guard():
                calls["n"] += 1
                return calls["n"] == 1

            ops2 = _FakeOps(rows=[_row("codex", LANE), _row("claude", LANE)])
            redrive = UC(ops=ops2, store=store, lease_guard=guard).run(
                _request_all_gates(), execute=True
            )
            self.assertTrue(redrive.lease_lost)
            self.assertTrue(redrive.is_blocked)
            self.assertTrue(redrive.redrive_blocked)
            self.assertFalse(redrive.executed)
            # the fence prevented the close despite the early guard passing.
            self.assertEqual(ops2.close_calls, [])
            self.assertGreaterEqual(calls["n"], 2)  # early guard + commit-point guard both ran
            self.assertEqual(self._disposition(store), DISPOSITION_HIBERNATED)

    def test_use_case_lease_guard_outcome_is_blocked_and_not_success(self):
        # R1-F2 at the use-case boundary: a commit-point lease loss is a typed blocked, zero-mutation
        # outcome (is_blocked True via the lease_lost property; is_success False; transition None).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
            SublaneHibernateUseCase as UC,
        )

        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = self._seed(home)
            outcome = UC(ops=_FakeOps(), store=store, lease_guard=lambda: False).run(
                _request_all_gates(), execute=True
            )
            self.assertTrue(outcome.lease_lost)
            self.assertTrue(outcome.is_blocked)
            self.assertFalse(outcome.is_success)
            self.assertIsNone(outcome.transition)
            self.assertEqual(self._disposition(store), DISPOSITION_ACTIVE)

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


class WorktreeBoundUseCaseSeamTest(unittest.TestCase):
    def test_an_unresolvable_worktree_is_a_typed_zero_call(self):
        # Review j#86726 R1-F2: use_case_fn returning None never touches any use case.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
            ATTEMPT_BLOCKED,
            LEG_REASON_WORKTREE_UNRESOLVED,
            run_hibernate_pass,
        )

        candidate = _candidate()
        result = run_hibernate_pass(
            [candidate],
            refresh_fn=lambda c: c,
            obligations_fn=lambda c: _obligations(),
            journal_fn=lambda c: JOURNAL,
            use_case_fn=lambda c: None,
            lease_renew_fn=lambda: True,
        )
        self.assertEqual(result.mutations, 0)
        attempt = result.attempts[0]
        self.assertEqual(attempt.kind, ATTEMPT_BLOCKED)
        self.assertEqual(attempt.reason, LEG_REASON_WORKTREE_UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
