"""herdr-native `workflow step` classifier + resolver tests (Redmine #13489).

Pins the pure herdr lane-role classification (from the launch-time sender identity +
workspace-registry project scope, per the documented shared-project-workspace model) and
the resolution-only outcome mapping for each role. The contract mirrors the tmux state
machine: every outcome is replayable (fixed ``state`` / ``execution`` / ``reason`` /
``next_owner``) and always names the next owner. Increment 1 is resolution-only — every
outcome carries ``primitive=none`` (no sublane lifecycle mutation, no delivery); the
policy-permitted auto-execution is increment 2, gated behind the task-level design mid-review.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    OWNER_CALLER,
    OWNER_CHILD,
    OWNER_GRANDCHILD,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDCHILD_REDMINE_WORK,
    STATE_GRANDPARENT_CONSULTATION,
    STATE_LANE_UNRESOLVED,
    STATE_PARENT_WORK_INTAKE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    REASON_HERDR_COORDINATOR_ORCHESTRATION,
    REASON_HERDR_LANE_ROLE_UNRESOLVED,
    REASON_HERDR_WORKER_DISPATCH_READY,
    REASON_HERDR_WORKER_SLOT_MISSING,
    REASON_HERDR_WORKER_STEP_READY,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)


class ClassifyHerdrWorkflowLaneTest(unittest.TestCase):
    """The herdr identity -> workflow-role mapping (no divergent identity model)."""

    def test_claude_any_lane_is_implementation_worker(self):
        for lane in ("issue_13489_herdr_workflow_step", "default", ""):
            lane_obj = classify_herdr_workflow_lane(
                provider="claude", lane_id=lane, project_scope="proj", repo_root="/w"
            )
            self.assertEqual(lane_obj.caller_role, ROLE_IMPLEMENTATION_WORKER)
            self.assertTrue(lane_obj.provider_safe)

    def test_codex_non_default_lane_is_delegated_coordinator(self):
        lane_obj = classify_herdr_workflow_lane(
            provider="codex", lane_id="issue_42", project_scope="", repo_root="/w"
        )
        self.assertEqual(lane_obj.caller_role, ROLE_DELEGATED_COORDINATOR)

    def test_codex_default_lane_with_scope_is_project_gateway(self):
        lane_obj = classify_herdr_workflow_lane(
            provider="codex", lane_id="default", project_scope="mozyo_bridge", repo_root="/w"
        )
        self.assertEqual(lane_obj.caller_role, ROLE_PROJECT_GATEWAY)

    def test_codex_default_lane_no_scope_is_grandparent(self):
        lane_obj = classify_herdr_workflow_lane(
            provider="codex", lane_id="", project_scope="", repo_root="/w"
        )
        self.assertEqual(lane_obj.caller_role, ROLE_GRANDPARENT_COORDINATOR)

    def test_unknown_provider_fails_closed(self):
        lane_obj = classify_herdr_workflow_lane(
            provider="gemini", lane_id="default", project_scope="p", repo_root="/w"
        )
        self.assertIsNone(lane_obj.caller_role)
        self.assertFalse(lane_obj.provider_safe)

    def test_locator_is_carried_as_self_pane_diagnostic_only(self):
        lane_obj = classify_herdr_workflow_lane(
            provider="claude", lane_id="x", project_scope="", repo_root="/w", locator="p42"
        )
        self.assertEqual(lane_obj.self_pane, "p42")


class ResolveHerdrWorkflowStepTest(unittest.TestCase):
    """Resolution-only outcome mapping per role (primitive=none throughout, increment 1)."""

    def _lane(self, provider, lane_id, scope=""):
        return classify_herdr_workflow_lane(
            provider=provider, lane_id=lane_id, project_scope=scope, repo_root="/w"
        )

    def test_worker_resolves_to_read_anchor_no_op(self):
        out = resolve_herdr_workflow_step(self._lane("claude", "issue_1"))
        self.assertEqual(out.state, STATE_GRANDCHILD_REDMINE_WORK)
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertEqual(out.next_owner, OWNER_GRANDCHILD)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)
        self.assertTrue(out.ok)

    def test_gateway_with_live_worker_resolves_to_dispatch_ready(self):
        out = resolve_herdr_workflow_step(
            self._lane("codex", "issue_1"), same_lane_worker_live=True
        )
        self.assertEqual(out.state, STATE_CHILD_WORKER_DISPATCH)
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_DISPATCH_READY)
        self.assertEqual(out.next_owner, OWNER_CHILD)
        self.assertIn("sublane dispatch-worker", out.next_action)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)

    def test_gateway_without_worker_fails_closed(self):
        out = resolve_herdr_workflow_step(
            self._lane("codex", "issue_1"), same_lane_worker_live=False
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_SLOT_MISSING)
        self.assertEqual(out.next_owner, OWNER_CHILD)
        self.assertFalse(out.ok)

    def test_gateway_inventory_unavailable_blocks_conservatively(self):
        out = resolve_herdr_workflow_step(
            self._lane("codex", "issue_1"), same_lane_worker_live=None
        )
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_SLOT_MISSING)
        self.assertIn("unavailable", out.detail)

    def test_project_gateway_resolves_to_coordinator_orchestration(self):
        out = resolve_herdr_workflow_step(self._lane("codex", "default", scope="mozyo_bridge"))
        self.assertEqual(out.state, STATE_PARENT_WORK_INTAKE)
        self.assertEqual(out.reason, REASON_HERDR_COORDINATOR_ORCHESTRATION)
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.next_owner, OWNER_CALLER)

    def test_grandparent_resolves_to_coordinator_orchestration(self):
        out = resolve_herdr_workflow_step(self._lane("codex", "default", scope=""))
        self.assertEqual(out.state, STATE_GRANDPARENT_CONSULTATION)
        self.assertEqual(out.reason, REASON_HERDR_COORDINATOR_ORCHESTRATION)

    def test_unresolved_lane_blocks_with_operator_owner(self):
        out = resolve_herdr_workflow_step(self._lane("gemini", "default"))
        self.assertEqual(out.state, STATE_LANE_UNRESOLVED)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_LANE_ROLE_UNRESOLVED)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)

    def test_every_increment1_outcome_is_resolution_only(self):
        # No leg auto-executes a primitive in increment 1.
        lanes = [
            (self._lane("claude", "issue_1"), None),
            (self._lane("codex", "issue_1"), True),
            (self._lane("codex", "issue_1"), False),
            (self._lane("codex", "default", scope="p"), None),
            (self._lane("gemini", "default"), None),
        ]
        for lane, worker_live in lanes:
            out = resolve_herdr_workflow_step(lane, same_lane_worker_live=worker_live)
            self.assertEqual(out.primitive, PRIMITIVE_NONE)
            self.assertFalse(out.executable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
