"""`workflow step` state-machine tests (Redmine #12755).

Pins the pure :func:`resolve_workflow_step` decision for each lane: grandparent
(ticketless consultation forward), parent (ticketless work-intake forward), child
(anchor-gated worker dispatch), grandchild (Redmine-governed work), the determined
callback rail, and the fail-closed states (ambiguous / missing / same-lane /
unsafe provider binding / self lane unresolved / anchor required). The contract is
that every outcome is replayable (fixed ``state`` / ``execution`` / ``reason`` /
``next_owner`` / ``primitive``) and always names the next owner — design
``vibes/docs/logics/workflow-step-command-design.md``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_NONE,
    CONFIDENCE_STRONG,
    TargetCandidate,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    EXECUTION_READY,
    OWNER_CALLER,
    OWNER_CHILD,
    OWNER_GRANDCHILD,
    OWNER_OPERATOR,
    OWNER_PARENT,
    PRIMITIVE_CHILD_INTAKE,
    PRIMITIVE_CONSULT,
    PRIMITIVE_HANDOFF_SEND,
    PRIMITIVE_NONE,
    PRIMITIVE_TICKETLESS_CALLBACK,
    REASON_ANCHOR_REQUIRED,
    REASON_CALLBACK_NOT_APPLICABLE,
    REASON_CALLBACK_OFF_RAIL,
    REASON_CALLBACK_READY,
    REASON_CALLER_AMBIGUOUS,
    REASON_CALLER_MISSING,
    REASON_CHILD_AMBIGUOUS,
    REASON_CONSULTATION_READY,
    REASON_GATEWAY_AMBIGUOUS,
    REASON_GATEWAY_MISSING,
    REASON_GATEWAY_NOT_COCKPIT_VISIBLE,
    REASON_REDMINE_WORK_READY,
    REASON_SAME_LANE_CHILD_ROUTE,
    REASON_SELF_LANE_UNRESOLVED,
    REASON_UNSAFE_PROVIDER_BINDING,
    REASON_WORKER_AMBIGUOUS,
    REASON_WORKER_DISPATCH_READY,
    REASON_WORKER_MISSING,
    REASON_WORKER_RUNS_WITHOUT_ANCHOR,
    REASON_WORK_INTAKE_READY,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDCHILD_REDMINE_WORK,
    STATE_GRANDPARENT_CONSULTATION,
    STATE_LANE_UNRESOLVED,
    STATE_PARENT_WORK_INTAKE,
    STATE_PENDING_CALLBACK,
    PendingCallback,
    WorkflowAnchor,
    WorkflowStepError,
    callback_rail_fields,
    classify_workflow_lane,
    resolve_workflow_step,
)

REPO = "/work/repo"
PROJECT = "cloud-drive"


def _cand(
    pane_id,
    *,
    role="codex",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    project_scope="",
    lane_kind="",
    view_kind=VIEW_KIND_COCKPIT_PANE,
    repo_root=REPO,
    session="gw",
):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source="pane_option",
        confidence=confidence,
        ambiguous=ambiguous,
        session=session,
        window_name="w",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws",
        workspace_label="ws",
        lane_id="lane",
        lane_label="lane",
        repo_short="repo",
        repo_root=repo_root,
        cwd=repo_root,
        host="host",
        view_kind=view_kind,
        branch=None,
        lane_kind=lane_kind,
        delegation_parent="",
        project_scope=project_scope,
        project_path="",
        project_label="",
    )


class LaneClassificationTest(unittest.TestCase):
    def test_grandparent_is_strong_codex_without_scope(self):
        lane = classify_workflow_lane(_cand("%g"), self_pane="%g")
        self.assertEqual(lane.caller_role, "grandparent_coordinator")
        self.assertTrue(lane.provider_safe)

    def test_parent_is_project_scoped_codex(self):
        lane = classify_workflow_lane(_cand("%p", project_scope=PROJECT), self_pane="%p")
        self.assertEqual(lane.caller_role, "project_gateway")

    def test_child_is_delegated_coordinator_stamp(self):
        lane = classify_workflow_lane(
            _cand("%c", project_scope=PROJECT, lane_kind="delegated_coordinator"),
            self_pane="%c",
        )
        self.assertEqual(lane.caller_role, "delegated_coordinator")

    def test_grandchild_is_claude(self):
        lane = classify_workflow_lane(_cand("%w", role="claude"), self_pane="%w")
        self.assertEqual(lane.caller_role, "implementation_worker")

    def test_weak_binding_is_unsafe(self):
        lane = classify_workflow_lane(
            _cand("%x", confidence=CONFIDENCE_NONE), self_pane="%x"
        )
        self.assertIsNone(lane.caller_role)
        self.assertFalse(lane.provider_safe)

    def test_self_not_discovered(self):
        lane = classify_workflow_lane(None, self_pane="%missing")
        self.assertIsNone(lane.caller_role)
        self.assertFalse(lane.provider_safe)


class GrandparentStepTest(unittest.TestCase):
    def test_unique_cockpit_gateway_consultation_ready(self):
        out = resolve_workflow_step(
            [_cand("%self"), _cand("%gw", project_scope=PROJECT)], self_pane="%self"
        )
        self.assertEqual(out.state, STATE_GRANDPARENT_CONSULTATION)
        self.assertEqual(out.execution, EXECUTION_READY)
        self.assertEqual(out.reason, REASON_CONSULTATION_READY)
        self.assertEqual(out.next_owner, OWNER_PARENT)
        self.assertEqual(out.primitive, PRIMITIVE_CONSULT)
        self.assertEqual(out.target_pane, "%gw")
        self.assertEqual(out.project_scope, PROJECT)
        self.assertTrue(out.executable)

    def test_no_gateway_missing(self):
        out = resolve_workflow_step([_cand("%self")], self_pane="%self")
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_GATEWAY_MISSING)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)
        self.assertFalse(out.ok)

    def test_multiple_gateways_ambiguous(self):
        out = resolve_workflow_step(
            [
                _cand("%self"),
                _cand("%gw1", project_scope=PROJECT),
                _cand("%gw2", project_scope="other"),
            ],
            self_pane="%self",
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_GATEWAY_AMBIGUOUS)

    def test_detached_gateway_not_cockpit_visible(self):
        out = resolve_workflow_step(
            [
                _cand("%self"),
                _cand("%gw", project_scope=PROJECT, view_kind=VIEW_KIND_NORMAL_WINDOW),
            ],
            self_pane="%self",
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_GATEWAY_NOT_COCKPIT_VISIBLE)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)


class ParentStepTest(unittest.TestCase):
    def test_distinct_child_work_intake_ready(self):
        out = resolve_workflow_step(
            [
                _cand("%self", project_scope=PROJECT),
                _cand("%child", project_scope=PROJECT),
            ],
            self_pane="%self",
        )
        self.assertEqual(out.state, STATE_PARENT_WORK_INTAKE)
        self.assertEqual(out.execution, EXECUTION_READY)
        self.assertEqual(out.reason, REASON_WORK_INTAKE_READY)
        self.assertEqual(out.next_owner, OWNER_CHILD)
        self.assertEqual(out.primitive, PRIMITIVE_CHILD_INTAKE)
        self.assertEqual(out.target_pane, "%child")

    def test_same_lane_only_self_blocked(self):
        out = resolve_workflow_step(
            [_cand("%self", project_scope=PROJECT)], self_pane="%self"
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_SAME_LANE_CHILD_ROUTE)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)

    def test_ambiguous_children_blocked(self):
        out = resolve_workflow_step(
            [
                _cand("%self", project_scope=PROJECT),
                _cand("%c1", project_scope=PROJECT, session="a"),
                _cand("%c2", project_scope=PROJECT, session="b"),
            ],
            self_pane="%self",
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_CHILD_AMBIGUOUS)


class ChildStepTest(unittest.TestCase):
    def _child(self):
        return _cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator")

    def _worker(self, pane="%wk"):
        return _cand(pane, role="claude", project_scope=PROJECT)

    def test_no_anchor_fails_closed_anchor_required(self):
        out = resolve_workflow_step(
            [self._child(), self._worker()], self_pane="%self"
        )
        self.assertEqual(out.state, STATE_CHILD_WORKER_DISPATCH)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_ANCHOR_REQUIRED)
        self.assertEqual(out.next_owner, OWNER_CHILD)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)
        self.assertEqual(out.durable_anchor, "none")
        self.assertFalse(out.executable)

    def test_with_anchor_and_worker_dispatch_ready_executable(self):
        out = resolve_workflow_step(
            [self._child(), self._worker()],
            self_pane="%self",
            anchor=WorkflowAnchor(issue="12755", journal="67549"),
        )
        self.assertEqual(out.execution, EXECUTION_READY)
        self.assertEqual(out.reason, REASON_WORKER_DISPATCH_READY)
        self.assertEqual(out.next_owner, OWNER_GRANDCHILD)
        self.assertEqual(out.primitive, PRIMITIVE_HANDOFF_SEND)
        self.assertEqual(out.target_pane, "%wk")
        self.assertEqual(out.durable_anchor, "redmine:issue=12755:journal=67549")
        # The anchored worker dispatch IS auto-executable once anchor + worker resolve.
        self.assertTrue(out.executable)

    def test_with_anchor_but_no_worker_fails_closed(self):
        out = resolve_workflow_step(
            [self._child()],
            self_pane="%self",
            anchor=WorkflowAnchor(issue="12755"),
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_WORKER_MISSING)
        self.assertEqual(out.next_owner, OWNER_CHILD)
        self.assertFalse(out.executable)

    def test_with_anchor_but_ambiguous_workers_fails_closed(self):
        out = resolve_workflow_step(
            [self._child(), self._worker("%wk1"), self._worker("%wk2")],
            self_pane="%self",
            anchor=WorkflowAnchor(issue="12755"),
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_WORKER_AMBIGUOUS)


class GrandchildStepTest(unittest.TestCase):
    def test_no_anchor_worker_runs_without_anchor(self):
        out = resolve_workflow_step([_cand("%self", role="claude")], self_pane="%self")
        self.assertEqual(out.state, STATE_GRANDCHILD_REDMINE_WORK)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_WORKER_RUNS_WITHOUT_ANCHOR)
        self.assertEqual(out.next_owner, OWNER_CHILD)

    def test_with_anchor_redmine_work_ready_noop(self):
        out = resolve_workflow_step(
            [_cand("%self", role="claude")],
            self_pane="%self",
            anchor=WorkflowAnchor(issue="12755"),
        )
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.reason, REASON_REDMINE_WORK_READY)
        self.assertEqual(out.next_owner, OWNER_GRANDCHILD)
        self.assertEqual(out.durable_anchor, "redmine:issue=12755")
        self.assertTrue(out.ok)


class CallbackStepTest(unittest.TestCase):
    def _grandparent(self, pane="%gp"):
        # The caller a project gateway returns up to: a strong Codex with no scope.
        return _cand(pane)

    def _gateway_caller(self, pane="%pgw"):
        # The caller a delegated coordinator returns up to: a project gateway.
        return _cand(pane, project_scope=PROJECT)

    def test_pending_callback_routes_to_resolved_caller_executable(self):
        out = resolve_workflow_step(
            [_cand("%self", project_scope=PROJECT), self._grandparent()],
            self_pane="%self",
            pending_callback=PendingCallback(classification="blocked"),
        )
        self.assertEqual(out.state, STATE_PENDING_CALLBACK)
        self.assertEqual(out.execution, EXECUTION_READY)
        self.assertEqual(out.reason, REASON_CALLBACK_READY)
        self.assertEqual(out.next_owner, OWNER_CALLER)
        self.assertEqual(out.primitive, PRIMITIVE_TICKETLESS_CALLBACK)
        self.assertEqual(out.callback_classification, "blocked")
        # A project gateway returns up to the grandparent coordinator; the resolved
        # caller pane is carried for the explicit --target.
        self.assertEqual(out.callback_to_role, "grandparent_coordinator")
        self.assertEqual(out.target_pane, "%gp")
        self.assertTrue(out.executable)

    def test_callback_from_child_returns_to_project_gateway_pane(self):
        out = resolve_workflow_step(
            [
                _cand("%self", project_scope=PROJECT, lane_kind="delegated_coordinator"),
                self._gateway_caller(),
            ],
            self_pane="%self",
            pending_callback=PendingCallback(classification="no_dispatch"),
        )
        self.assertEqual(out.callback_to_role, "project_gateway")
        self.assertEqual(out.target_pane, "%pgw")
        self.assertTrue(out.executable)

    def test_caller_missing_fails_closed(self):
        out = resolve_workflow_step(
            [_cand("%self", project_scope=PROJECT)],  # no grandparent caller present
            self_pane="%self",
            pending_callback=PendingCallback(classification="blocked"),
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_CALLER_MISSING)
        self.assertFalse(out.executable)

    def test_caller_ambiguous_fails_closed(self):
        out = resolve_workflow_step(
            [
                _cand("%self", project_scope=PROJECT),
                self._grandparent("%gp1"),
                self._grandparent("%gp2"),
            ],
            self_pane="%self",
            pending_callback=PendingCallback(classification="blocked"),
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_CALLER_AMBIGUOUS)

    def test_callback_at_grandparent_not_applicable(self):
        # The grandparent is the terminal recorder; it has no ticketless caller above.
        out = resolve_workflow_step(
            [_cand("%self"), _cand("%gw", project_scope=PROJECT)],
            self_pane="%self",
            pending_callback=PendingCallback(classification="blocked"),
        )
        self.assertEqual(out.state, STATE_PENDING_CALLBACK)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_CALLBACK_NOT_APPLICABLE)

    def test_callback_takes_priority_over_forward_step(self):
        # A pending callback is routed even on a lane that would otherwise forward.
        out = resolve_workflow_step(
            [
                _cand("%self", project_scope=PROJECT),  # would otherwise child-intake
                _cand("%child", project_scope=PROJECT),
                self._grandparent(),
            ],
            self_pane="%self",
            pending_callback=PendingCallback(classification="anchor_required"),
        )
        self.assertEqual(out.state, STATE_PENDING_CALLBACK)
        self.assertEqual(out.primitive, PRIMITIVE_TICKETLESS_CALLBACK)

    def test_off_rail_classification_fails_closed(self):
        # review_ready is an anchored review path, not a no-anchor callback class.
        out = resolve_workflow_step(
            [_cand("%self", project_scope=PROJECT), self._grandparent()],
            self_pane="%self",
            pending_callback=PendingCallback(classification="review_ready"),
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_CALLBACK_OFF_RAIL)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)

    def test_callback_rail_fields_mapping(self):
        fields = callback_rail_fields("anchor_required")
        self.assertEqual(fields["dispatch_decision"], "anchor_required_before_worker_dispatch")
        self.assertEqual(fields["workflow_next_owner"], "caller")
        with self.assertRaises(WorkflowStepError):
            callback_rail_fields("review_ready")


class BlockedLaneTest(unittest.TestCase):
    def test_unsafe_provider_binding(self):
        out = resolve_workflow_step(
            [_cand("%self", confidence=CONFIDENCE_NONE)], self_pane="%self"
        )
        self.assertEqual(out.state, STATE_LANE_UNRESOLVED)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_UNSAFE_PROVIDER_BINDING)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)

    def test_self_lane_unresolved(self):
        out = resolve_workflow_step(
            [_cand("%other", project_scope=PROJECT)], self_pane="%missing"
        )
        self.assertEqual(out.state, STATE_LANE_UNRESOLVED)
        self.assertEqual(out.reason, REASON_SELF_LANE_UNRESOLVED)

    def test_payload_is_replayable(self):
        out = resolve_workflow_step([_cand("%self")], self_pane="%self")
        payload = out.as_payload()
        for key in (
            "state",
            "next_action",
            "execution",
            "reason",
            "next_owner",
            "primitive",
            "durable_anchor",
        ):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
