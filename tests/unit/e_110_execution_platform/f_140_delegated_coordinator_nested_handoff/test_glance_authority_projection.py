"""Unit tests for the glance authority / execution-surface / reconcile projection (#13758).

Pins the fail-closed fixed-token contract of
:mod:`...domain.glance_authority_projection` and its join onto the existing
:class:`...domain.workflow_glance.WorkflowGlanceRow` (owner intent j#78309 authority
visibility, j#78321 execution-surface provenance, j#78002 reconcile projection):

- every field validates to a bounded token; out-of-vocabulary -> ``unknown`` (never guessed);
- a ``verified`` identity is only honoured on a ``managed_sublane`` surface (§ provenance);
- the reconcile projection is derived from a ``reconcile_state`` record, ``None`` -> empty;
- the row payload emits the three groups without demoting ``workflow_state``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_authority_projection import (
    DISPATCH_STATE_DISPATCHED,
    DISPATCH_STATE_UNKNOWN,
    EXECUTION_SURFACE_INTERNAL_TASK_AGENT,
    EXECUTION_SURFACE_MANAGED_SUBLANE,
    EXECUTION_SURFACE_UNKNOWN,
    AuthorityFacts,
    ExecutionSurfaceFacts,
    ReconcileFacts,
    facts_from_lifecycle_record,
    reconcile_facts_from_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    LaneSignal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    IssueGlanceSnapshot,
    fold_glance_row,
)


class ReconcileFactsTest(unittest.TestCase):
    def test_bool_attempt_folds_to_zero(self):
        facts = ReconcileFacts(reconcile_attempt=True).validated()  # bool is not a count
        self.assertEqual(facts.reconcile_attempt, 0)

    def test_negative_attempt_folds_to_zero(self):
        self.assertEqual(ReconcileFacts(reconcile_attempt=-3).validated().reconcile_attempt, 0)

    def test_payload_has_the_six_reconcile_fields(self):
        facts = ReconcileFacts(
            expected_gate="implementation_done",
            expected_owner="implementation_worker",
            reconcile_attempt=2,
            deadline="2026-07-16T00:00:00Z",
            last_disposition="reconcile_self_heal_attempt_2",
            escalated=False,
        )
        p = facts.as_payload()
        self.assertEqual(
            set(p),
            {
                "expected_gate",
                "expected_owner",
                "reconcile_attempt",
                "deadline",
                "last_disposition",
                "escalated",
            },
        )
        self.assertEqual(p["reconcile_attempt"], 2)


class _FakeRecord:
    expected_gate = "review_request"
    expected_next_owner = "implementation_gateway"
    reconcile_failure_count = 3
    deadline = "2026-07-16T01:00:00Z"
    last_disposition = "reconcile_three_strike"
    escalated = True


class ReconcileFromRecordTest(unittest.TestCase):
    def test_none_record_is_empty_facts(self):
        facts = reconcile_facts_from_record(None)
        self.assertEqual(facts.expected_gate, "")
        self.assertEqual(facts.reconcile_attempt, 0)
        self.assertFalse(facts.escalated)

    def test_record_projects_all_fields(self):
        facts = reconcile_facts_from_record(_FakeRecord())
        self.assertEqual(facts.expected_gate, "review_request")
        self.assertEqual(facts.expected_owner, "implementation_gateway")
        self.assertEqual(facts.reconcile_attempt, 3)
        self.assertTrue(facts.escalated)


class AuthorityFactsTest(unittest.TestCase):
    def test_blank_role_provider_default_to_unknown(self):
        facts = AuthorityFacts().validated()
        self.assertEqual(facts.active_execution_role, "unknown")
        self.assertEqual(facts.active_provider, "unknown")

    def test_bool_actor_count_folds_to_zero(self):
        self.assertEqual(
            AuthorityFacts(concurrent_actor_count=True).validated().concurrent_actor_count, 0
        )

    def test_carries_authority_anchor_and_transition(self):
        facts = AuthorityFacts(
            active_execution_role="project_gateway",
            active_provider="codex",
            authority_anchor="13758:78309",
            authority_generation="4",
            superseded_authority_generation="3",
            transition_reason="codex_direct_edit",
            concurrent_actor_count=1,
            worktree_mutation_attribution="superseded_generation",
        ).validated()
        self.assertEqual(facts.active_provider, "codex")
        self.assertEqual(facts.authority_anchor, "13758:78309")
        self.assertEqual(facts.concurrent_actor_count, 1)


class ExecutionSurfaceFactsTest(unittest.TestCase):
    def test_out_of_vocab_surface_folds_to_unknown(self):
        self.assertEqual(
            ExecutionSurfaceFacts(execution_surface="nonsense").validated().execution_surface,
            EXECUTION_SURFACE_UNKNOWN,
        )

    def test_verified_only_honoured_on_managed_sublane(self):
        # An internal task agent can never carry a verified lane identity (j#78321).
        facts = ExecutionSurfaceFacts(
            execution_surface=EXECUTION_SURFACE_INTERNAL_TASK_AGENT,
            managed_lane_identity_verified=True,
        ).validated()
        self.assertFalse(facts.managed_lane_identity_verified)

    def test_capacity_fails_closed_off_managed_sublane(self):
        # review F5: an internal task agent / unverified surface can never be capacity-eligible.
        internal = ExecutionSurfaceFacts(
            execution_surface=EXECUTION_SURFACE_INTERNAL_TASK_AGENT,
            productive_capacity_eligible=True,
        ).validated()
        self.assertFalse(internal.productive_capacity_eligible)
        # managed_sublane but identity NOT verified -> also fail-closed.
        unverified = ExecutionSurfaceFacts(
            execution_surface=EXECUTION_SURFACE_MANAGED_SUBLANE,
            managed_lane_identity_verified=False,
            productive_capacity_eligible=True,
        ).validated()
        self.assertFalse(unverified.productive_capacity_eligible)

    def test_verified_kept_on_managed_sublane(self):
        facts = ExecutionSurfaceFacts(
            execution_surface=EXECUTION_SURFACE_MANAGED_SUBLANE,
            managed_lane_identity_verified=True,
            gateway_dispatch_state=DISPATCH_STATE_DISPATCHED,
            worker_dispatch_state="garbage",
            productive_capacity_eligible=True,
        ).validated()
        self.assertTrue(facts.managed_lane_identity_verified)
        self.assertEqual(facts.gateway_dispatch_state, DISPATCH_STATE_DISPATCHED)
        self.assertEqual(facts.worker_dispatch_state, DISPATCH_STATE_UNKNOWN)  # fail-closed
        self.assertTrue(facts.productive_capacity_eligible)


class _FakeLifecycleRecord:
    def __init__(self, **kw):
        self.binding_kind = kw.get("binding_kind", "issue")
        self.decision_issue_id = kw.get("decision_issue_id", "13758")
        self.decision_journal = kw.get("decision_journal", "79337")
        self.worktree_identity = kw.get("worktree_identity", "wt-abc")
        self.declared_slots = kw.get("declared_slots", "slots-json")
        self.lane_generation = kw.get("lane_generation", 2)
        self.revision = kw.get("revision", 5)


class FactsFromLifecycleTest(unittest.TestCase):
    def test_none_record_is_empty(self):
        authority, execution = facts_from_lifecycle_record(None)
        self.assertEqual(authority.active_provider, "unknown")
        self.assertEqual(execution.execution_surface, "unknown")

    def test_projects_durable_provenance_only_never_active_actor_facts(self):
        # review R3-F3: the lifecycle record projects DURABLE provenance; the live-actor facts
        # (role / provider / count / capacity) are NEVER promoted from ownership metadata.
        authority, execution = facts_from_lifecycle_record(_FakeLifecycleRecord())
        # durable provenance:
        self.assertEqual(authority.authority_anchor, "13758:79337")
        self.assertEqual(authority.authority_generation, "2")
        self.assertEqual(execution.execution_surface, EXECUTION_SURFACE_MANAGED_SUBLANE)
        self.assertTrue(execution.managed_lane_identity_verified)  # worktree + slots present
        self.assertEqual(execution.lane_lifecycle_revision, "5")
        # live-actor facts stay fail-closed (never fabricated from binding_kind ownership):
        self.assertEqual(authority.active_execution_role, "unknown")
        self.assertEqual(authority.active_provider, "unknown")
        self.assertEqual(authority.concurrent_actor_count, 0)
        self.assertFalse(execution.productive_capacity_eligible)  # needs live dispatch state
        self.assertEqual(execution.gateway_dispatch_state, DISPATCH_STATE_UNKNOWN)
        self.assertEqual(authority.superseded_authority_generation, "")

    def test_incomplete_binding_is_unverified(self):
        _, execution = facts_from_lifecycle_record(
            _FakeLifecycleRecord(declared_slots="")  # pins-only gap -> not verified
        )
        self.assertFalse(execution.managed_lane_identity_verified)
        self.assertFalse(execution.productive_capacity_eligible)

    def test_project_gateway_binding_does_not_fabricate_a_gateway_actor(self):
        # binding_kind is ownership, not the active actor -> active role stays unknown.
        authority, _ = facts_from_lifecycle_record(
            _FakeLifecycleRecord(binding_kind="project_gateway")
        )
        self.assertEqual(authority.active_execution_role, "unknown")


class FoldJoinTest(unittest.TestCase):
    def test_fold_emits_the_three_groups_without_demoting_state(self):
        snap = IssueGlanceSnapshot(
            issue_id="13758",
            signal=LaneSignal(issue="13758", latest_gate="review_request"),
            reconcile=ReconcileFacts(expected_owner="implementation_worker", reconcile_attempt=1),
            authority=AuthorityFacts(active_provider="claude"),
            execution=ExecutionSurfaceFacts(
                execution_surface=EXECUTION_SURFACE_INTERNAL_TASK_AGENT,
                managed_lane_identity_verified=True,
            ),
        )
        row = fold_glance_row(snap)
        payload = row.as_payload()
        self.assertIn("reconcile", payload)
        self.assertIn("authority", payload)
        self.assertIn("execution_surface", payload)
        self.assertEqual(payload["reconcile"]["expected_owner"], "implementation_worker")
        self.assertEqual(payload["authority"]["active_provider"], "claude")
        # the internal-task-agent verified flag was failed closed in the join.
        self.assertFalse(payload["execution_surface"]["managed_lane_identity_verified"])
        # the workflow_state is still the durable-record classification (not demoted).
        self.assertEqual(row.workflow_state, row.state_class)

    def test_default_snapshot_folds_to_fail_closed_groups(self):
        # A producer that does not fill the groups yields unknown / blank, never fabricated.
        snap = IssueGlanceSnapshot(issue_id="1", signal=LaneSignal(issue="1"))
        payload = fold_glance_row(snap).as_payload()
        self.assertEqual(payload["authority"]["active_execution_role"], "unknown")
        self.assertEqual(payload["execution_surface"]["execution_surface"], "unknown")
        self.assertEqual(payload["reconcile"]["reconcile_attempt"], 0)


if __name__ == "__main__":
    unittest.main()
