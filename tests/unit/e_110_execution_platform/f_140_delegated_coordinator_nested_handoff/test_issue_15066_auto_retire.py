"""Automatic finished-lane retire selection/budget tests (Redmine #15066)."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_supervisor_wiring import (  # noqa: E501
    RETIRE_CANDIDATE_AMBIGUOUS,
    RETIRE_LEASE_LOST,
    RETIRE_SNAPSHOT_CHANGED,
    RetireCandidateSnapshot,
    build_retire_leg,
    default_retire_leg_fn,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E501
    RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
    RETIRE_RESULT_BLOCKED,
    RETIRE_RESULT_RETIRED,
    RetireApplicationRequest,
    RetireApplicationResult,
    RetireAssertions,
    RetireIdentity,
    run_retire_application,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit import (  # noqa: E501
    read_issue_closed_states,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_retire_leg import (  # noqa: E501
    mark_pass_budget,
    run_folded_retire,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_RETIRE_BUDGET_DEFERRED,
    SUPERVISION_BOUNDED_RECONCILIATION,
    WorkspaceSupervisionOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E501
    RebootLaneFacts,
)


def candidate(**overrides) -> RetireCandidateSnapshot:
    values = dict(
        workspace="ws_1",
        issue="15066",
        lane="issue_15066_auto_retire",
        lane_generation=3,
        revision=9,
        lane_disposition="hibernated",
        worktree="",
        branch="issue_15066_auto_retire",
        integration_branch="main",
        intent=RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
        decision_journal="100701",
        integration_journal="100705",
        review_request_journal="100700",
        review_head="a" * 40,
        integration_source_head="a" * 40,
        integration_head="b" * 40,
        origin_tip="c" * 40,
        ci_run="31100000000",
        issue_state_known=True,
        issue_closed=True,
        inventory_known=True,
        callback_debt_known=True,
        callbacks_drained=True,
        task_close_journal="100701",
        owner_gates_resolved=True,
        review_admissible=True,
        integration_confirmed=True,
        integration_ci_green=True,
        worktree_clean=True,
        source_head_matches_branch=True,
        origin_reachable=True,
    )
    values.update(overrides)
    return RetireCandidateSnapshot(**values)


class _JsonResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self):
        return self._body


class BatchedIssueStateReadTest(unittest.TestCase):
    def _credential_patches(self):
        creds = SimpleNamespace(api_key="test-key", base_url="https://redmine.test")
        return (
            mock.patch(
                "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials.resolve_redmine_credentials",
                return_value=creds,
            ),
            mock.patch(
                "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_context.normalize_base_url",
                return_value="https://redmine.test",
            ),
        )

    def test_reads_multiple_issue_states_with_one_redirect_refusing_seam(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return _JsonResponse(
                {
                    "issues": [
                        {"id": 15066, "status": {"is_closed": True}},
                        {"id": 15067, "status": {"is_closed": False}},
                    ],
                    "total_count": 2,
                    "offset": 0,
                    "limit": 2,
                }
            )

        credential_patch, context_patch = self._credential_patches()
        with credential_patch, context_patch:
            states = read_issue_closed_states(
                ["15067", "15066"], opener=opener, environ={}
            )

        self.assertEqual(states, {"15066": True, "15067": False})
        self.assertEqual(len(requests), 1)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(requests[0][0].full_url).query
        )
        self.assertEqual(query["issue_id"], ["15066,15067"])
        self.assertEqual(query["status_id"], ["*"])
        self.assertEqual(query["limit"], ["2"])

    def test_truncated_batch_is_entirely_unknown(self):
        def opener(_request, _timeout):
            return _JsonResponse(
                {
                    "issues": [{"id": 15066, "status": {"is_closed": True}}],
                    "total_count": 2,
                    "offset": 0,
                    "limit": 1,
                }
            )

        credential_patch, context_patch = self._credential_patches()
        with credential_patch, context_patch:
            states = read_issue_closed_states(
                ["15066", "15067"], opener=opener, environ={}
            )

        self.assertEqual(states, {"15066": None, "15067": None})

    def test_transport_failure_is_unknown_without_retrying_per_issue(self):
        calls = []

        def opener(request, _timeout):
            calls.append(request)
            raise urllib.error.URLError("unavailable")

        credential_patch, context_patch = self._credential_patches()
        with credential_patch, context_patch:
            states = read_issue_closed_states(
                ["15066", "15067"], opener=opener, environ={}
            )

        self.assertEqual(states, {"15066": None, "15067": None})
        self.assertEqual(len(calls), 1)

    def test_large_frontier_is_chunked_at_redmine_page_limit(self):
        request_sizes = []

        def opener(request, _timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            ids = query["issue_id"][0].split(",")
            request_sizes.append(len(ids))
            return _JsonResponse(
                {
                    "issues": [
                        {"id": int(issue), "status": {"is_closed": False}}
                        for issue in ids
                    ],
                    "total_count": len(ids),
                    "offset": 0,
                    "limit": len(ids),
                }
            )

        wanted = [str(issue) for issue in range(1, 206)]
        credential_patch, context_patch = self._credential_patches()
        with credential_patch, context_patch:
            states = read_issue_closed_states(wanted, opener=opener, environ={})

        self.assertEqual(request_sizes, [100, 100, 5])
        self.assertEqual(states, {issue: False for issue in wanted})

    def test_ambiguous_closed_frontier_skips_per_candidate_journal_reads(self):
        records = tuple(
            SimpleNamespace(
                repo_workspace_id="ws_1",
                issue_id=issue,
                lane_disposition="active",
                process_release="not_requested",
            )
            for issue in ("15066", "15067")
        )
        facts = tuple(
            RebootLaneFacts(
                workspace_id="ws_1",
                lane_id=f"issue_{issue}",
                issue_id=issue,
                recorded_worktree="",
                branch=f"issue_{issue}",
                worktree_present=False,
                branch_exists=True,
                head_integrated=False,
                issue_closed=None,
                slots=(),
            )
            for issue in ("15066", "15067")
        )
        journal_factory = mock.Mock(
            side_effect=AssertionError("ambiguity must stop before journal reads")
        )
        config = SimpleNamespace(
            sublane_integration=SimpleNamespace(integration_branch="main")
        )
        with mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore"
        ), mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle_readonly.load_lane_lifecycle_readonly",
            return_value=records,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition.load_committed_repo_local_config",
            return_value=config,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition.live_journal_reader",
            journal_factory,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit.gather_reboot_facts",
            return_value=facts,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit.read_issue_closed_states",
            return_value={"15066": True, "15067": True},
        ):
            result = default_retire_leg_fn(home=Path("/state"))(
                SimpleNamespace(
                    workspace_id="ws_1", canonical_path="/repo"
                ),
                lambda: True,
                {"mutated": False, "uncertain": False},
            )

        self.assertEqual(result.attempts[0].reason, RETIRE_CANDIDATE_AMBIGUOUS)
        journal_factory.assert_not_called()


class AutomaticRetireSelectionTest(unittest.TestCase):
    def test_application_request_separates_close_and_integration_anchors(self):
        wanted = candidate()
        request = wanted.application_request(repo_root=Path("/repo"))

        self.assertEqual(request.journal, wanted.task_close_journal)
        self.assertEqual(request.integration_journal, wanted.integration_journal)

    def test_action_time_owner_issue_mismatch_blocks_before_actuation(self):
        expected = candidate().identity
        request = RetireApplicationRequest(
            repo_root=Path("/repo"),
            issue=expected.issue,
            lane_label=expected.lane,
            assertions=RetireAssertions(
                issue_closed=True,
                callbacks_drained=True,
                verification_passed=True,
                durable_record_recorded=True,
                target_identity_known=True,
                latest_generation_admissible=True,
            ),
            intent=RETIRE_INTENT_HIBERNATED_UNBOUND_LIVE_ZERO,
            expected_identity=RetireIdentity(
                workspace=expected.workspace,
                issue=expected.issue,
                lane=expected.lane,
                lane_generation=expected.lane_generation,
                revision=expected.revision,
            ),
        )
        measured = SimpleNamespace(
            workspace=expected.workspace,
            issue="different-owner",
            lane=expected.lane,
            lane_generation=expected.lane_generation,
            revision=expected.revision,
        )
        with mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility.resolve_retire_evidence_target",
            return_value=measured,
        ):
            result = run_retire_application(request)

        self.assertEqual(result.state, RETIRE_RESULT_BLOCKED)
        self.assertEqual(result.reason, "retire_identity_changed")
        self.assertFalse(result.mutated)

    def test_exact_two_snapshots_then_one_retire(self):
        calls = []
        retired = []
        wanted = candidate()

        def snapshots(ws, restrict):
            calls.append((ws.workspace_id, restrict))
            return (wanted,)

        def retire(request):
            retired.append(request)
            return RetireApplicationResult(
                state=RETIRE_RESULT_RETIRED, mutated=True
            )

        state_home = Path("/state-home")
        leg = build_retire_leg(
            snapshot_fn=snapshots, retire_fn=retire, home=state_home
        )
        result = leg(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
            restrict_issues=frozenset({"15066"}),
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].home, state_home)
        self.assertEqual(retired[0].expected_identity, wanted.identity)
        self.assertEqual(result.mutations, 1)
        self.assertEqual(result.attempts[0].cleanup_state, "cleanup_blocked")
        self.assertEqual(result.attempts[0].cleanup_reason, "cleanup_atomic_guard_unavailable")

    def test_two_eligible_candidates_are_typed_blocked(self):
        one = candidate()
        two = candidate(issue="15067", lane="issue_15067_other")
        retired = []
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (one, two),
            retire_fn=lambda request: retired.append(request),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(retired, [])
        self.assertEqual(result.attempts[0].reason, RETIRE_CANDIDATE_AMBIGUOUS)

    def test_blocked_candidate_is_not_hidden_from_ambiguity_check(self):
        one = candidate()
        two = candidate(
            issue="15067", lane="issue_15067_other", integration_ci_green=False
        )
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (one, two),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(result.attempts[0].reason, RETIRE_CANDIDATE_AMBIGUOUS)

    def test_snapshot_change_is_zero_mutation(self):
        snapshots = iter(((candidate(),), (candidate(revision=10),)))
        retired = []
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: next(snapshots),
            retire_fn=lambda request: retired.append(request),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(retired, [])
        self.assertEqual(result.attempts[0].reason, RETIRE_SNAPSHOT_CHANGED)

    def test_fresh_origin_tip_change_is_snapshot_change(self):
        snapshots = iter(
            ((candidate(origin_tip="c" * 40),), (candidate(origin_tip="d" * 40),))
        )
        retired = []
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: next(snapshots),
            retire_fn=lambda request: retired.append(request),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(retired, [])
        self.assertEqual(result.attempts[0].reason, RETIRE_SNAPSHOT_CHANGED)

    def test_lost_lease_stops_before_second_snapshot(self):
        count = 0

        def snapshots(_ws, _restrict):
            nonlocal count
            count += 1
            return (candidate(),)

        result = build_retire_leg(snapshot_fn=snapshots)(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: False,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(count, 1)
        self.assertEqual(result.attempts[0].reason, RETIRE_LEASE_LOST)

    def test_lost_lease_after_second_snapshot_stops_before_retire(self):
        renews = iter((True, False))
        retired = []
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (candidate(),),
            retire_fn=lambda request: retired.append(request),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: next(renews),
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(retired, [])
        self.assertEqual(result.attempts[0].reason, RETIRE_LEASE_LOST)

    def test_inadmissible_candidate_is_typed_blocked(self):
        retired = []
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (candidate(integration_ci_green=False),),
            retire_fn=lambda request: retired.append(request),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(retired, [])
        self.assertEqual(result.attempts[0].reason, "integration_ci_unsettled")

    def test_unreadable_issue_state_is_typed_blocked(self):
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (
                candidate(issue_state_known=False, issue_closed=False),
            ),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(result.attempts[0].reason, "issue_state_unreadable")

    def test_unreadable_inventory_is_typed_blocked(self):
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (
                candidate(inventory_known=False),
            ),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(result.attempts[0].reason, "inventory_unreadable")

    def test_closed_issue_without_task_close_is_typed_blocked(self):
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (
                candidate(task_close_journal=""),
            ),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(result.attempts[0].reason, "task_close_missing")

    def test_unreadable_callback_debt_is_distinct_from_live_debt(self):
        result = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (
                candidate(callback_debt_known=False, callbacks_drained=False),
            ),
        )(
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            lambda: True,
            {"mutated": False, "uncertain": False},
        )
        self.assertEqual(result.attempts[0].reason, "callback_debt_unreadable")

    def test_empty_injected_lifecycle_frontier_avoids_global_store_read(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_reboot_audit as audit,
        )

        state_home = Path("/isolated-state")
        with mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_herdr_projection.repo_scope_workspace_id",
            return_value="ws_1",
        ) as workspace_id, mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle_readonly."
            "load_lane_lifecycle_readonly",
            side_effect=AssertionError("global lifecycle store must not be reread"),
        ):
            facts = audit.gather_reboot_facts(
                Path("/repo"), home=state_home, lifecycle_rows=()
            )

        self.assertEqual(facts, ())
        workspace_id.assert_called_once_with(Path("/repo"), home=state_home)


class FoldedRetireBudgetTest(unittest.TestCase):
    def _base_outcome(self):
        return WorkspaceSupervisionOutcome(
            workspace_id="ws_1", lease_acquired=True, lease_reason="acquired"
        )

    def test_mutation_spends_budget_before_hibernate(self):
        leg = build_retire_leg(
            snapshot_fn=lambda _ws, _restrict: (candidate(),),
            retire_fn=lambda _request: RetireApplicationResult(
                state=RETIRE_RESULT_RETIRED, mutated=True
            ),
        )
        sup = SimpleNamespace(_retire_leg_fn=leg)
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        outcome = run_folded_retire(
            sup,
            SimpleNamespace(workspace_id="ws_1", canonical_path=str(Path("/repo"))),
            self._base_outcome(),
            mode=SUPERVISION_BOUNDED_RECONCILIATION,
            pass_budget=budget,
            bound_issues=(),
            renew=lambda: True,
        )
        mark_pass_budget(budget, outcome)
        self.assertEqual(outcome.retire_mutations, 1)
        self.assertTrue(budget["mutated"])

    def test_prior_mutation_defers_without_calling_leg(self):
        called = []
        sup = SimpleNamespace(_retire_leg_fn=lambda *a, **k: called.append((a, k)))
        outcome = run_folded_retire(
            sup,
            SimpleNamespace(workspace_id="ws_1", canonical_path="/repo"),
            self._base_outcome(),
            mode=SUPERVISION_BOUNDED_RECONCILIATION,
            pass_budget={"mutated": True, "uncertain": False},
            bound_issues=(),
            renew=lambda: True,
        )
        self.assertEqual(called, [])
        self.assertEqual(outcome.retire_disposition, SKIP_RETIRE_BUDGET_DEFERRED)


if __name__ == "__main__":
    unittest.main()
