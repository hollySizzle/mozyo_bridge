"""herdr-native `workflow step` classifier + resolver tests (Redmine #13489).

Pins the pure herdr lane-role classification and the resolution-only outcome mapping after
the mid-review corrections (j#74748 / j#74749 / j#74750): only non-default lane slots get a
lane-local class (codex -> gateway, claude -> worker); the default-lane pair and an unknown
provider fail closed; a worker / gateway lane is anchor-gated; and the same-lane worker
liveness is a 0 / 1 / 2+ cardinality. Every increment-1 outcome carries ``primitive=none``.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    OWNER_CHILD,
    OWNER_GRANDCHILD,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_CHILD_WORKER_DISPATCH,
    STATE_GRANDCHILD_REDMINE_WORK,
    STATE_LANE_UNRESOLVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_AMBIGUOUS,
    ANCHOR_MISSING,
    ANCHOR_RETIRED,
    ANCHOR_UNVERIFIED,
    ANCHOR_VERIFIED,
    REASON_HERDR_ANCHOR_AMBIGUOUS,
    REASON_HERDR_ANCHOR_UNRESOLVED,
    REASON_HERDR_ANCHOR_UNVERIFIED,
    REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED,
    REASON_HERDR_LANE_ROLE_UNRESOLVED,
    REASON_HERDR_WORKER_AMBIGUOUS,
    REASON_HERDR_WORKER_DISPATCH_READY,
    REASON_HERDR_WORKER_LOCATOR_MISSING,
    REASON_HERDR_WORKER_SLOT_MISSING,
    REASON_HERDR_WORKER_STEP_READY,
    WORKER_ABSENT,
    WORKER_AMBIGUOUS,
    WORKER_LIVE,
    WORKER_LOCATOR_MISSING,
    WORKER_UNAVAILABLE,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)

VERIFIED_PTR = "redmine:issue=13489"


class ClassifyHerdrWorkflowLaneTest(unittest.TestCase):
    """Only non-default lane slots get a role; default-lane / unknown provider fail closed."""

    def test_claude_non_default_lane_is_implementation_worker(self):
        lane = classify_herdr_workflow_lane(
            provider="claude", lane_id="issue_1", repo_root="/w"
        )
        self.assertEqual(lane.caller_role, ROLE_IMPLEMENTATION_WORKER)
        self.assertTrue(lane.provider_safe)

    def test_codex_non_default_lane_is_delegated_coordinator(self):
        lane = classify_herdr_workflow_lane(provider="codex", lane_id="issue_1", repo_root="/w")
        self.assertEqual(lane.caller_role, ROLE_DELEGATED_COORDINATOR)

    def test_default_lane_codex_fails_closed(self):
        lane = classify_herdr_workflow_lane(provider="codex", lane_id="default", repo_root="/w")
        self.assertIsNone(lane.caller_role)
        self.assertFalse(lane.provider_safe)

    def test_default_lane_claude_fails_closed_not_worker(self):
        # A default-lane Claude is the coordinator's assistant, never an implementation worker.
        lane = classify_herdr_workflow_lane(provider="claude", lane_id="", repo_root="/w")
        self.assertIsNone(lane.caller_role)

    def test_unknown_provider_fails_closed(self):
        lane = classify_herdr_workflow_lane(provider="gemini", lane_id="issue_1", repo_root="/w")
        self.assertIsNone(lane.caller_role)

    def test_no_project_scope_authority_is_carried(self):
        # F1: project scope is never derived here (the registry project_name heuristic is gone).
        lane = classify_herdr_workflow_lane(provider="codex", lane_id="issue_1", repo_root="/w")
        self.assertEqual(lane.project_scope, "")


class ResolveDefaultAndUnknownTest(unittest.TestCase):
    def test_default_lane_blocks_with_ambiguous_coordinator_role(self):
        lane = classify_herdr_workflow_lane(provider="codex", lane_id="default", repo_root="/w")
        out = resolve_herdr_workflow_step(lane)
        self.assertEqual(out.state, STATE_LANE_UNRESOLVED)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)
        self.assertEqual(out.next_owner, OWNER_OPERATOR)

    def test_unknown_provider_blocks_with_lane_role_unresolved(self):
        lane = classify_herdr_workflow_lane(provider="gemini", lane_id="issue_1", repo_root="/w")
        out = resolve_herdr_workflow_step(lane)
        self.assertEqual(out.reason, REASON_HERDR_LANE_ROLE_UNRESOLVED)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)


class ResolveWorkerLaneTest(unittest.TestCase):
    def _lane(self):
        return classify_herdr_workflow_lane(provider="claude", lane_id="issue_1", repo_root="/w")

    def test_verified_anchor_resolves_to_read_and_implement(self):
        out = resolve_herdr_workflow_step(
            self._lane(), anchor_status=ANCHOR_VERIFIED, anchor_pointer=VERIFIED_PTR
        )
        self.assertEqual(out.state, STATE_GRANDCHILD_REDMINE_WORK)
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertEqual(out.next_owner, OWNER_GRANDCHILD)
        self.assertEqual(out.durable_anchor, VERIFIED_PTR)
        self.assertEqual(out.primitive, PRIMITIVE_NONE)

    def test_missing_anchor_fails_closed(self):
        out = resolve_herdr_workflow_step(self._lane(), anchor_status=ANCHOR_MISSING)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)
        self.assertEqual(out.durable_anchor, "none")

    def test_ambiguous_anchor_fails_closed(self):
        out = resolve_herdr_workflow_step(self._lane(), anchor_status=ANCHOR_AMBIGUOUS)
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_AMBIGUOUS)

    def test_retired_anchor_fails_closed(self):
        out = resolve_herdr_workflow_step(self._lane(), anchor_status=ANCHOR_RETIRED)
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)

    def test_unverified_anchor_fails_closed(self):
        out = resolve_herdr_workflow_step(self._lane(), anchor_status=ANCHOR_UNVERIFIED)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNVERIFIED)

    def test_no_anchor_status_fails_closed(self):
        out = resolve_herdr_workflow_step(self._lane())
        self.assertEqual(out.execution, EXECUTION_BLOCKED)


class ResolveGatewayLaneTest(unittest.TestCase):
    def _lane(self):
        return classify_herdr_workflow_lane(provider="codex", lane_id="issue_1", repo_root="/w")

    def _run(self, worker_liveness, anchor_status=ANCHOR_VERIFIED):
        return resolve_herdr_workflow_step(
            self._lane(),
            worker_liveness=worker_liveness,
            anchor_status=anchor_status,
            anchor_pointer=VERIFIED_PTR,
        )

    def test_verified_anchor_single_live_worker_is_dispatch_ready(self):
        out = self._run(WORKER_LIVE)
        self.assertEqual(out.state, STATE_CHILD_WORKER_DISPATCH)
        self.assertEqual(out.execution, EXECUTION_NO_OP)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_DISPATCH_READY)
        self.assertEqual(out.durable_anchor, VERIFIED_PTR)
        self.assertIn("sublane dispatch-worker", out.next_action)

    def test_duplicate_worker_is_ambiguous_fail_closed(self):
        out = self._run(WORKER_AMBIGUOUS)
        self.assertEqual(out.execution, EXECUTION_BLOCKED)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_AMBIGUOUS)

    def test_absent_worker_is_slot_missing(self):
        out = self._run(WORKER_ABSENT)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_SLOT_MISSING)

    def test_locator_missing_worker_fails_closed(self):
        out = self._run(WORKER_LOCATOR_MISSING)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_LOCATOR_MISSING)

    def test_unavailable_inventory_is_slot_missing_conservative(self):
        out = self._run(WORKER_UNAVAILABLE)
        self.assertEqual(out.reason, REASON_HERDR_WORKER_SLOT_MISSING)
        self.assertIn("unavailable", out.detail)

    def test_gateway_missing_anchor_fails_closed_before_worker_gate(self):
        out = self._run(WORKER_LIVE, anchor_status=ANCHOR_MISSING)
        self.assertEqual(out.reason, REASON_HERDR_ANCHOR_UNRESOLVED)


class ResolutionOnlyInvariantTest(unittest.TestCase):
    def test_no_increment1_outcome_is_executable(self):
        cases = [
            resolve_herdr_workflow_step(
                classify_herdr_workflow_lane(provider="claude", lane_id="l", repo_root="/w"),
                anchor_status=ANCHOR_VERIFIED,
                anchor_pointer=VERIFIED_PTR,
            ),
            resolve_herdr_workflow_step(
                classify_herdr_workflow_lane(provider="codex", lane_id="l", repo_root="/w"),
                worker_liveness=WORKER_LIVE,
                anchor_status=ANCHOR_VERIFIED,
                anchor_pointer=VERIFIED_PTR,
            ),
            resolve_herdr_workflow_step(
                classify_herdr_workflow_lane(provider="codex", lane_id="default", repo_root="/w")
            ),
        ]
        for out in cases:
            self.assertEqual(out.primitive, PRIMITIVE_NONE)
            self.assertFalse(out.executable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
