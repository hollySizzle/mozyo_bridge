"""Same-lane-guarded parent -> child intake route resolution (Redmine #12748).

Pins the pure :func:`resolve_child_intake_route`: the same-lane guard (the child
route must not resolve back to the parent's own lane), and the four fail-closed /
forward classifications the ``project-gateway child-intake`` rail dispatches on
(child_resolved / same_lane / child_missing / child_ambiguous). The caller's own
lane (``caller_pane``) is a negative self-fence, never the target authority.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.child_intake_route import (
    STATUS_CHILD_AMBIGUOUS,
    STATUS_CHILD_MISSING,
    STATUS_CHILD_RESOLVED,
    STATUS_SAME_LANE,
    ChildIntakeRouteError,
    resolve_child_intake_route,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"


def _candidate(
    pane_id,
    *,
    role="codex",
    repo_root=REPO,
    project_scope=PROJECT,
    session="proj",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
):
    return TargetCandidate(
        pane_id=pane_id, role=role, role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=confidence, ambiguous=ambiguous, session=session,
        window_name="cockpit", window_index="0", pane_index="0", active=False,
        workspace_id="ws", workspace_label="gk", lane_id="default", lane_label=None,
        repo_short="gk-3500-it-operations", repo_root=repo_root,
        cwd=f"{repo_root}/projects/{project_scope}", host="local",
        view_kind=VIEW_KIND_COCKPIT_PANE, branch="main",
        project_scope=project_scope, project_path=f"projects/{project_scope}",
        project_label="label",
    )


def _resolve(candidates, *, caller_pane="%parent", session=None):
    return resolve_child_intake_route(
        candidates,
        repo_root=REPO,
        project_scope=PROJECT,
        caller_pane=caller_pane,
        session=session,
    )


class ChildIntakeRouteTest(unittest.TestCase):
    def test_distinct_child_resolves_and_excludes_self(self):
        # Two coordinator lanes: the caller's own (%parent) + a distinct child.
        route = _resolve(
            [_candidate("%parent"), _candidate("%child")], caller_pane="%parent"
        )
        self.assertEqual(route.status, STATUS_CHILD_RESOLVED)
        self.assertTrue(route.ok)
        self.assertIsNotNone(route.selected)
        # The resolved child is the OTHER lane, never the caller's own.
        self.assertEqual(route.selected.pane_id, "%child")
        self.assertTrue(route.self_is_gateway)
        # The intake is no-anchor: the route never demands an anchor itself.
        self.assertFalse(route.anchor_required)

    def test_only_caller_lane_is_same_lane_blocked(self):
        # The only coordinator lane is the caller itself -> the child route resolved
        # back to the parent. Refuse to adopt the parent as its own child.
        route = _resolve([_candidate("%parent")], caller_pane="%parent")
        self.assertEqual(route.status, STATUS_SAME_LANE)
        self.assertFalse(route.ok)
        self.assertIsNone(route.selected)
        self.assertTrue(route.self_is_gateway)
        self.assertIn("resolved back to the parent", route.detail)

    def test_no_coordinator_lane_is_child_missing(self):
        # The caller's own lane is not even discoverable as a coordinator (only a
        # claude worker is up) -> genuinely no child lane; launch a distinct one.
        route = _resolve(
            [_candidate("%worker", role="claude")], caller_pane="%parent"
        )
        self.assertEqual(route.status, STATUS_CHILD_MISSING)
        self.assertFalse(route.ok)
        self.assertFalse(route.self_is_gateway)

    def test_multiple_distinct_children_are_ambiguous(self):
        # Caller + two distinct coordinator lanes that the resolver cannot tell
        # apart -> ambiguous (refuse to adopt or launch).
        route = _resolve(
            [_candidate("%parent"), _candidate("%c1"), _candidate("%c2")],
            caller_pane="%parent",
        )
        self.assertEqual(route.status, STATUS_CHILD_AMBIGUOUS)
        self.assertFalse(route.ok)
        self.assertIsNone(route.selected)

    def test_caller_pane_is_only_a_self_fence_not_the_target(self):
        # The distinct child is resolved by identity; the caller_pane only excludes
        # the parent. The selected child is never the caller_pane.
        route = _resolve(
            [_candidate("%parent"), _candidate("%child")], caller_pane="%parent"
        )
        self.assertEqual(route.status, STATUS_CHILD_RESOLVED)
        self.assertNotEqual(route.selected.pane_id, route.caller_pane)

    def test_empty_caller_pane_fails_closed(self):
        with self.assertRaises(ChildIntakeRouteError):
            _resolve([_candidate("%child")], caller_pane="")

    def test_payload_shape(self):
        payload = _resolve(
            [_candidate("%parent"), _candidate("%child")], caller_pane="%parent"
        ).as_payload()
        self.assertEqual(payload["status"], STATUS_CHILD_RESOLVED)
        self.assertFalse(payload["anchor_required"])
        self.assertEqual(payload["caller_pane"], "%parent")
        self.assertEqual(payload["selected"]["runtime"]["pane_id"], "%child")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
