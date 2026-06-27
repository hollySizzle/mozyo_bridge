"""Current-Unit relative delegation route + cockpit-visible startup evidence (Redmine #12699).

Covers the pure layer #12699 adds on top of #12668 / #12708:

- the relative delegation slice (current Unit is the anchor; one step down resolves
  ``project_gateway`` / ``delegated_coordinator`` / ``implementation_worker``),
  reusing :func:`resolve_launch_or_adopt` for the coordinator-class targets and the
  anchor-gated dispatch contract for the worker;
- the startup-evidence classifier that keeps a ``cockpit --json`` preview and a
  detached ``--no-attach`` normal session out of the green path; and
- the linkage that makes a detached / preview startup ``insufficient`` (never PASS)
  through the existing #12558 final-classification oracle.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    ACTION_BLOCKED,
    ACTION_LAUNCH,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    POSITION_GRANDPARENT,
    POSITION_PARENT,
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
    STARTUP_COCKPIT_VISIBLE,
    STARTUP_DETACHED_NO_ATTACH,
    STARTUP_JSON_PREVIEW,
    STARTUP_NONE,
    TARGET_DELEGATED_COORDINATOR,
    TARGET_IMPLEMENTATION_WORKER,
    TARGET_PROJECT_GATEWAY,
    RelativeRouteError,
    classify_startup_evidence,
    cockpit_visible_from_candidate,
    resolve_relative_route,
    resolve_relative_step,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_records import (
    CLASS_INSUFFICIENT,
    CLASS_PASS,
    ClassificationInputs,
    classify_final,
)

REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"
PROJECT_PATH = "projects/giken-cloud-drive-management"


def _candidate(
    pane_id,
    *,
    role="codex",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    session="dept-root",
    repo_root=REPO,
    project_scope=PROJECT,
    view_kind=VIEW_KIND_COCKPIT_PANE,
):
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source=ROLE_SOURCE_PANE_OPTION,
        confidence=confidence,
        ambiguous=ambiguous,
        session=session,
        window_name="cockpit",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id="ws-gk3500",
        workspace_label="gk-3500-it-operations",
        lane_id="default",
        lane_label=None,
        repo_short="gk-3500-it-operations",
        repo_root=repo_root,
        cwd=f"{repo_root}/{PROJECT_PATH}",
        host="local",
        view_kind=view_kind,
        branch="main",
        project_scope=project_scope,
        project_path=PROJECT_PATH,
        project_label="クラウドドライブ管理",
    )


class RelativeStepTest(unittest.TestCase):
    def test_grandparent_delegates_to_project_gateway(self) -> None:
        step = resolve_relative_step(ROLE_GRANDPARENT_COORDINATOR)
        self.assertEqual(step.caller_position, POSITION_GRANDPARENT)
        self.assertEqual(step.target_position, POSITION_PARENT)
        self.assertEqual(step.target_binding, TARGET_PROJECT_GATEWAY)
        self.assertTrue(step.coordinator_class)
        # The ticketless consultation hop needs no anchor (doc work-item boundary).
        self.assertFalse(step.anchor_required)

    def test_parent_delegates_to_delegated_coordinator(self) -> None:
        step = resolve_relative_step(ROLE_PROJECT_GATEWAY)
        self.assertEqual(step.target_binding, TARGET_DELEGATED_COORDINATOR)
        self.assertTrue(step.coordinator_class)
        self.assertTrue(step.anchor_required)

    def test_child_delegates_to_implementation_worker(self) -> None:
        step = resolve_relative_step(ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(step.target_binding, TARGET_IMPLEMENTATION_WORKER)
        self.assertFalse(step.coordinator_class)
        self.assertTrue(step.anchor_required)

    def test_grandchild_worker_has_no_downward_delegation(self) -> None:
        # A worker callbacks up; it never delegates down -> fail closed.
        with self.assertRaises(RelativeRouteError):
            resolve_relative_step(ROLE_IMPLEMENTATION_WORKER)

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(RelativeRouteError):
            resolve_relative_step("owner")


class StartupEvidenceTest(unittest.TestCase):
    def test_json_preview_is_not_green_path(self) -> None:
        ev = classify_startup_evidence(preview_only=True, cockpit_visible=True)
        # Even if a preview *reports* membership, a preview does not mutate.
        self.assertEqual(ev.mode, STARTUP_JSON_PREVIEW)
        self.assertFalse(ev.is_green_path)

    def test_cockpit_visible_is_green_path(self) -> None:
        ev = classify_startup_evidence(cockpit_visible=True, session_present=True)
        self.assertEqual(ev.mode, STARTUP_COCKPIT_VISIBLE)
        self.assertTrue(ev.is_green_path)

    def test_detached_no_attach_session_is_not_green_path(self) -> None:
        ev = classify_startup_evidence(cockpit_visible=False, session_present=True)
        self.assertEqual(ev.mode, STARTUP_DETACHED_NO_ATTACH)
        self.assertFalse(ev.is_green_path)

    def test_nothing_observed_fails_closed(self) -> None:
        ev = classify_startup_evidence()
        self.assertEqual(ev.mode, STARTUP_NONE)
        self.assertFalse(ev.is_green_path)

    def test_cockpit_visible_from_candidate(self) -> None:
        self.assertTrue(cockpit_visible_from_candidate(_candidate("%gw")))
        self.assertFalse(
            cockpit_visible_from_candidate(
                _candidate("%norm", view_kind=VIEW_KIND_NORMAL_WINDOW)
            )
        )
        self.assertFalse(cockpit_visible_from_candidate(None))


class ResolveRelativeRouteTest(unittest.TestCase):
    def _route(self, candidates, *, caller_role=ROLE_GRANDPARENT_COORDINATOR, session=None):
        return resolve_relative_route(
            candidates,
            caller_role=caller_role,
            repo_root=REPO,
            project_scope=PROJECT,
            session=session,
        )

    def test_grandparent_found_cockpit_lane_is_green_adopt(self) -> None:
        plan = self._route([_candidate("%gw"), _candidate("%w", role="claude")])
        self.assertIsNotNone(plan.launch_or_adopt)
        self.assertEqual(plan.launch_or_adopt.action, ACTION_ADOPT)
        self.assertTrue(plan.ok)
        self.assertTrue(plan.green_path)
        self.assertEqual(plan.startup_evidence.mode, STARTUP_COCKPIT_VISIBLE)

    def test_grandparent_adopts_normal_window_lane_but_not_green(self) -> None:
        # A matching coordinator exists but it is a detached normal window: a real
        # lane (adopt) yet NOT cockpit-visible green-path evidence (#12699).
        plan = self._route([_candidate("%norm", view_kind=VIEW_KIND_NORMAL_WINDOW)])
        self.assertEqual(plan.launch_or_adopt.action, ACTION_ADOPT)
        self.assertTrue(plan.ok)
        self.assertFalse(plan.green_path)
        self.assertEqual(plan.startup_evidence.mode, STARTUP_DETACHED_NO_ATTACH)
        self.assertIn("cockpit", plan.next_action)

    def test_grandparent_missing_yields_cockpit_visible_launch(self) -> None:
        plan = self._route([_candidate("%w", role="claude")])
        self.assertEqual(plan.launch_or_adopt.action, ACTION_LAUNCH)
        self.assertTrue(plan.ok)
        self.assertFalse(plan.green_path)
        self.assertIn("cockpit", plan.next_action)
        # Names the cockpit-visible startup, refuses the detached / preview escape.
        self.assertIn("--no-attach", plan.next_action)

    def test_grandparent_ambiguous_is_blocked(self) -> None:
        plan = self._route(
            [_candidate("%gw1", session="a"), _candidate("%gw2", session="b")]
        )
        self.assertEqual(plan.launch_or_adopt.action, ACTION_BLOCKED)
        self.assertFalse(plan.ok)
        self.assertFalse(plan.green_path)

    def test_parent_resolves_delegated_coordinator_as_coordinator_class(self) -> None:
        plan = self._route([_candidate("%coord")], caller_role=ROLE_PROJECT_GATEWAY)
        self.assertEqual(plan.step.target_binding, TARGET_DELEGATED_COORDINATOR)
        self.assertEqual(plan.launch_or_adopt.action, ACTION_ADOPT)
        self.assertTrue(plan.anchor_required)

    def test_child_worker_is_anchor_gated_not_launched(self) -> None:
        plan = self._route(
            [_candidate("%w", role="claude")], caller_role=ROLE_DELEGATED_COORDINATOR
        )
        self.assertEqual(plan.step.target_binding, TARGET_IMPLEMENTATION_WORKER)
        # A worker is dispatched against a Redmine anchor, never launched-or-adopted.
        self.assertIsNone(plan.launch_or_adopt)
        self.assertTrue(plan.anchor_required)
        self.assertEqual(plan.startup_evidence.mode, STARTUP_NONE)
        self.assertIn("handoff send", plan.next_action)
        self.assertIn("--source redmine", plan.next_action)

    def test_payload_round_trips(self) -> None:
        plan = self._route([_candidate("%gw")])
        payload = plan.as_payload()
        self.assertTrue(payload["green_path"])
        self.assertEqual(payload["step"]["target_binding"], TARGET_PROJECT_GATEWAY)
        self.assertEqual(payload["startup_evidence"]["mode"], STARTUP_COCKPIT_VISIBLE)


class StartupEvidenceClassificationLinkageTest(unittest.TestCase):
    """A detached / preview startup is insufficient (never PASS) via the #12558 oracle."""

    def _classify(self, evidence):
        # The startup evidence gates whether the route fully realized.
        inputs = ClassificationInputs(route_fully_realized=evidence.is_green_path)
        return classify_final(inputs)

    def test_detached_no_attach_json_ready_is_insufficient_not_pass(self) -> None:
        # The #12698 escape: `mozyo --repo ... --no-attach --json` is preview-only.
        ev = classify_startup_evidence(preview_only=True)
        verdict, reason = self._classify(ev)
        self.assertEqual(verdict, CLASS_INSUFFICIENT)
        self.assertNotEqual(verdict, CLASS_PASS)
        self.assertEqual(reason, "route_not_fully_realized")

    def test_detached_normal_session_is_insufficient_not_pass(self) -> None:
        ev = classify_startup_evidence(cockpit_visible=False, session_present=True)
        verdict, _ = self._classify(ev)
        self.assertEqual(verdict, CLASS_INSUFFICIENT)

    def test_cockpit_visible_startup_can_reach_pass(self) -> None:
        ev = classify_startup_evidence(cockpit_visible=True, session_present=True)
        verdict, _ = self._classify(ev)
        self.assertEqual(verdict, CLASS_PASS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
