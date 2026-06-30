"""Ticketless project-gateway UX readiness scenarios (Redmine #12669).

Parent Feature #12667 ``ticketless project gateway UX を実機フィードバックから
再設計する``. These classical (Detroit-school) scenarios drive the **real** pure
planners the GK3500 three-tier gateway UX is built from — composing the #12668
semantic gateway resolver, the #12708 launch-or-adopt selector, the #12699
current-Unit relative route, the #12706 grandparent/gateway transition-role
boundary, and the #12740 forward ticketless consultation — end to end. Every step
is a pure function, so nothing is faked: the scenarios assert the durable,
fail-closed behaviour the issue's acceptance list enumerates, so a later #12709
real-machine rerun decision can stand on a green source-runtime readiness gate
instead of operator pane hand-selection.

Each test class maps to one acceptance scenario from #12669:

- root consultation classifies a project from *routing metadata only*, never the
  active pane / pane id (``RootClassifiesByRoutingMetadataOnlyScenarioTest``);
- the root (grandparent) does **not** run project-domain docs / rclone / mount /
  web research / implementation file resolution — the transition-role boundary
  forbids it and the consultation hop carries no Redmine anchor
  (``RootDoesNoDomainWorkScenarioTest``);
- ``gateway_missing`` returns the standard cockpit-visible launch action
  (``GatewayMissingReturnsLaunchActionScenarioTest``);
- a found gateway is handed the ticketless consultation through the semantic
  target selector (``GatewayFoundForwardsConsultationScenarioTest``);
- an ambiguous gateway fails closed with the candidates and the resolution path,
  never a silent pick (``GatewayAmbiguousFailsClosedScenarioTest``);
- the implementation worker lane is reached only behind a Redmine issue/journal
  anchor (``ImplementationNeedsRedmineAnchorScenarioTest``);
- the whole green path is expressible on the standard route with **no** ``%pane``
  direct addressing (``StandardPathNeedsNoPaneIdScenarioTest``).

This module is cross-cutting (it spans the ``f_120`` discovery/route and ``f_130``
handoff-routing contexts), so per the tests-placement policy it lives in
``tests/scenarios/`` and is not subdivided by bounded context.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the repo-local ``src`` and the ``tests`` package root importable for
# isolated / single-file discovery (harmless under full discover).
_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_PANE_OPTION,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (  # noqa: E402
    REASON_PROJECT_SCOPE_MISMATCH,
    REASON_REPO_ROOT_MISMATCH,
    REASON_ROLE_MISMATCH,
    STATUS_FOUND,
    STATUS_GATEWAY_AMBIGUOUS,
    STATUS_GATEWAY_MISSING,
    ProjectGatewayRoute,
    resolve_project_gateway,
    start_project_gateway_command,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.child_intake_route import (  # noqa: E402
    STATUS_CHILD_RESOLVED,
    STATUS_SAME_LANE,
    resolve_child_intake_route,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (  # noqa: E402
    ACTION_ADOPT,
    ACTION_BLOCKED,
    ACTION_LAUNCH,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E402
    POSITION_GRANDCHILD,
    POSITION_PARENT,
    ROLE_DELEGATED_COORDINATOR,
    STARTUP_COCKPIT_VISIBLE,
    STARTUP_NONE,
    resolve_relative_route,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (  # noqa: E402
    CALLBACK_VIA_TICKETLESS_CALLBACK,
    CONSULTATION_PROJECT_DOMAIN,
    CONSULTATION_ROUTING,
    TicketlessConsultation,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (  # noqa: E402
    ROLE_DELEGATED_COORDINATOR as ROLE_CHILD_COORDINATOR,
    WORK_SHAPE_DOMAIN_DESIGN,
    TicketlessWorkIntake,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (  # noqa: E402
    PROJECT_DOMAIN_DECISIONS,
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
    resolve_transition_role,
)

# The GK3500 binding: a cloud-drive-management project under the department-root
# IT-operations workspace. The grandparent (department root) consults the parent
# project gateway about this scope.
REPO = "/work/gk-3500-it-operations"
PROJECT = "giken-cloud-drive-management"
PROJECT_PATH = "projects/giken-cloud-drive-management"
DEPT_SESSION = "dept-root"


def _candidate(
    pane_id,
    *,
    role="codex",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    session=DEPT_SESSION,
    repo_root=REPO,
    project_scope=PROJECT,
    project_path=PROJECT_PATH,
    view_kind=VIEW_KIND_COCKPIT_PANE,
    active=False,
    lane_id="default",
):
    """Build a project-gateway-shaped :class:`TargetCandidate` (mirrors #12668 tests)."""
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
        active=active,
        workspace_id="ws-gk3500",
        workspace_label="gk-3500-it-operations",
        lane_id=lane_id,
        lane_label=None,
        repo_short="gk-3500-it-operations",
        repo_root=repo_root,
        cwd=f"{repo_root}/{project_path}" if project_path else repo_root,
        host="local",
        view_kind=view_kind,
        branch="main",
        project_scope=project_scope,
        project_path=project_path,
        project_label="クラウドドライブ管理",
    )


def _route(*, session=None):
    """The semantic department-root -> project-gateway route (no pane id)."""
    return ProjectGatewayRoute(
        repo_root=REPO, project_scope=PROJECT, session=session
    )


class RootClassifiesByRoutingMetadataOnlyScenarioTest(unittest.TestCase):
    """The root classifies the project from routing metadata, never the active pane."""

    def test_identity_match_resolves_regardless_of_active_or_pane_id(self) -> None:
        # The identity-matching gateway is INACTIVE; an active pane in a different
        # repo is present. Selection must follow routing metadata (role + repo_root
        # + project_scope), not the active flag or pane id.
        gateway = _candidate("%30", active=False)
        active_other = _candidate(
            "%31", active=True, repo_root="/work/some-other-repo"
        )
        resolution = resolve_project_gateway([active_other, gateway], _route())
        self.assertEqual(STATUS_FOUND, resolution.status)
        self.assertIsNotNone(resolution.selected)
        self.assertEqual("%30", resolution.selected.pane_id)
        self.assertFalse(resolution.selected.active)
        # The active off-repo pane was declined, by repo-root identity, not chosen.
        reasons = {nm.reason for nm in resolution.near_misses}
        self.assertIn(REASON_REPO_ROOT_MISMATCH, reasons)

    def test_pane_id_is_never_the_tiebreaker(self) -> None:
        # Two candidates with the SAME routing identity but different pane ids are
        # ambiguous — the resolver never breaks the tie on pane id / active.
        resolution = resolve_project_gateway(
            [_candidate("%40", lane_id="a"), _candidate("%41", lane_id="b", active=True)],
            _route(),
        )
        self.assertEqual(STATUS_GATEWAY_AMBIGUOUS, resolution.status)
        self.assertIsNone(resolution.selected)

    def test_classification_is_an_allowed_grandparent_action(self) -> None:
        # Classifying the consultation is explicitly within the grandparent's
        # bounded routing role (it is allowed to read routing metadata).
        boundary = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)
        self.assertIn("classify_ticketless_consultation", boundary.allowed_actions)


class RootDoesNoDomainWorkScenarioTest(unittest.TestCase):
    """The root forwards routing only; it runs no project-domain / probe work."""

    def test_grandparent_boundary_forbids_domain_and_probe_actions(self) -> None:
        boundary = resolve_transition_role(ROLE_GRANDPARENT_COORDINATOR)
        for forbidden in (
            "project_domain_decision",
            "local_probe",
            "implementation",
            "direct_Claude_send",
        ):
            self.assertIn(forbidden, boundary.forbidden_actions)
        # The two project-domain decisions are owned by the gateway, never the root.
        for decision in PROJECT_DOMAIN_DECISIONS:
            self.assertIn(decision, boundary.forbidden_actions)
        self.assertEqual(ROLE_PROJECT_GATEWAY, boundary.handoff_target_role)

    def test_gateway_owns_what_the_grandparent_must_not_decide(self) -> None:
        # Complementary boundary: the project gateway is allowed exactly the
        # domain decisions the grandparent is forbidden, so nothing is left
        # un-owned (and the root cannot pre-empt them).
        gateway = resolve_transition_role(ROLE_PROJECT_GATEWAY)
        for decision in PROJECT_DOMAIN_DECISIONS:
            self.assertIn(decision, gateway.allowed_actions)

    def test_grandparent_to_gateway_hop_carries_no_anchor(self) -> None:
        # The consultation hop (grandparent -> project gateway) does NOT require a
        # Redmine anchor — it is a routing consultation, not a worker dispatch — so
        # no implementation work item is minted just to ask the gateway.
        plan = resolve_relative_route(
            [_candidate("%30")],
            caller_role=ROLE_GRANDPARENT_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertEqual(POSITION_PARENT, plan.step.target_position)
        self.assertFalse(plan.step.anchor_required)
        self.assertFalse(plan.anchor_required)


class GatewayMissingReturnsLaunchActionScenarioTest(unittest.TestCase):
    """No live gateway -> gateway_missing + the standard cockpit-visible launch."""

    def test_no_candidate_classifies_gateway_missing(self) -> None:
        resolution = resolve_project_gateway([], _route())
        self.assertEqual(STATUS_GATEWAY_MISSING, resolution.status)
        self.assertIsNone(resolution.selected)

    def test_launch_command_is_cockpit_visible_and_project_rooted(self) -> None:
        resolution = resolve_project_gateway([], _route())
        command = start_project_gateway_command(
            resolution.route, project_path=PROJECT_PATH
        )
        # The standard startup is run from the project workdir and is declared
        # cockpit-visible. The trailing ``#`` comment legitimately *names* the
        # forbidden escapes ("do NOT add --repo", "NOT a --no-attach / --json
        # preview"), so the executable portion (before the comment) is what must
        # be free of a --repo root column / --no-attach / --json preview.
        runnable = command.split("#", 1)[0]
        self.assertIn("mozyo-bridge cockpit", runnable)
        self.assertIn(PROJECT_PATH, runnable)
        self.assertNotIn("--repo", runnable)
        self.assertNotIn("--no-attach", runnable)
        self.assertNotIn("--json", runnable)
        self.assertIn("startup=cockpit_visible", command)

    def test_relative_route_with_no_live_gateway_plans_a_launch(self) -> None:
        # The grandparent route over an empty candidate list resolves to LAUNCH,
        # and nothing is green-path yet (nothing live to adopt).
        plan = resolve_relative_route(
            [],
            caller_role=ROLE_GRANDPARENT_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertIsNotNone(plan.launch_or_adopt)
        self.assertEqual(ACTION_LAUNCH, plan.launch_or_adopt.action)
        self.assertIn("mozyo-bridge cockpit", plan.next_action)
        self.assertFalse(plan.green_path)


class GatewayFoundForwardsConsultationScenarioTest(unittest.TestCase):
    """A found gateway is handed the ticketless consultation via the semantic selector."""

    def test_single_gateway_is_found_and_adopted(self) -> None:
        gateway = _candidate("%30")
        resolution = resolve_project_gateway([gateway], _route())
        self.assertEqual(STATUS_FOUND, resolution.status)
        self.assertEqual(gateway, resolution.selected)
        # As a relative route the same live lane is adopted, cockpit-visible green.
        plan = resolve_relative_route(
            [gateway],
            caller_role=ROLE_GRANDPARENT_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertEqual(ACTION_ADOPT, plan.launch_or_adopt.action)
        self.assertEqual(STARTUP_COCKPIT_VISIBLE, plan.startup_evidence.mode)
        self.assertTrue(plan.green_path)

    def test_forwarded_consultation_keeps_the_worker_anchor_gate(self) -> None:
        # The consultation forwarded to the found gateway is a no-anchor forward
        # rail, yet it carries the fixed worker-dispatch-requires-anchor invariant
        # so the gateway cannot read it as permission to dispatch a worker.
        consultation = TicketlessConsultation(
            consultation_kind=CONSULTATION_PROJECT_DOMAIN,
            callback_to_role=ROLE_GRANDPARENT_COORDINATOR,
            callback_methods=(CALLBACK_VIA_TICKETLESS_CALLBACK,),
            read_contract=ROLE_PROJECT_GATEWAY,
        )
        self.assertTrue(consultation.worker_dispatch_requires_anchor)
        payload = consultation.to_structured_dict()
        self.assertTrue(payload["worker_dispatch_requires_anchor"])
        self.assertEqual(ROLE_GRANDPARENT_COORDINATOR, payload["callback_to_role"])


class GatewayAmbiguousFailsClosedScenarioTest(unittest.TestCase):
    """Two matching gateways -> fail closed with the candidates and the resolution."""

    def test_ambiguous_returns_both_candidates_and_no_silent_pick(self) -> None:
        a = _candidate("%30", lane_id="a")
        b = _candidate("%31", lane_id="b")
        resolution = resolve_project_gateway([a, b], _route())
        self.assertEqual(STATUS_GATEWAY_AMBIGUOUS, resolution.status)
        self.assertIsNone(resolution.selected)
        self.assertEqual({a, b}, set(resolution.matched))
        # The detail names how to resolve it (narrow with --session / disambiguate).
        self.assertIn("--session", resolution.detail)

    def test_relative_route_blocks_on_ambiguous_gateway(self) -> None:
        plan = resolve_relative_route(
            [_candidate("%30", lane_id="a"), _candidate("%31", lane_id="b")],
            caller_role=ROLE_GRANDPARENT_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertEqual(ACTION_BLOCKED, plan.launch_or_adopt.action)
        self.assertFalse(plan.ok)
        self.assertFalse(plan.green_path)

    def test_a_session_filter_disambiguates_the_two_gateways(self) -> None:
        # The fail-closed resolution is recoverable exactly as its detail says:
        # narrowing the route by session selects the one in that session.
        here = _candidate("%30", lane_id="a", session="cockpit-a")
        there = _candidate("%31", lane_id="b", session="cockpit-b")
        resolution = resolve_project_gateway(
            [here, there], _route(session="cockpit-a")
        )
        self.assertEqual(STATUS_FOUND, resolution.status)
        self.assertEqual(here, resolution.selected)


class ImplementationNeedsRedmineAnchorScenarioTest(unittest.TestCase):
    """The implementation worker lane is reached only behind a Redmine anchor."""

    def test_worker_hop_is_anchor_gated_and_never_launched(self) -> None:
        # From the child (delegated coordinator), the one-step-down target is the
        # grandchild implementation worker: anchor-required, and NOT launched/adopted
        # as a cockpit gateway (it is dispatched against the anchor).
        plan = resolve_relative_route(
            [],
            caller_role=ROLE_DELEGATED_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertEqual(POSITION_GRANDCHILD, plan.step.target_position)
        self.assertTrue(plan.step.anchor_required)
        self.assertTrue(plan.anchor_required)
        self.assertIsNone(plan.launch_or_adopt)
        self.assertEqual(STARTUP_NONE, plan.startup_evidence.mode)
        # The next action names the standard Redmine-anchored dispatch rail.
        self.assertIn("ensure_redmine_anchor", plan.next_action)
        self.assertIn("--source redmine", plan.next_action)
        self.assertIn("--issue", plan.next_action)
        self.assertIn("--journal", plan.next_action)
        # The worker hop is anchor-gated, so it is never green-path route evidence.
        self.assertFalse(plan.green_path)

    def test_routing_consultation_cannot_smuggle_a_relaxed_anchor_gate(self) -> None:
        # Even a pure routing consultation (the lightest forward class) keeps the
        # worker-dispatch anchor invariant true — the no-anchor rail cannot express
        # an anchored worker dispatch.
        consultation = TicketlessConsultation(
            consultation_kind=CONSULTATION_ROUTING,
            callback_to_role=ROLE_GRANDPARENT_COORDINATOR,
            callback_methods=(CALLBACK_VIA_TICKETLESS_CALLBACK,),
            read_contract=ROLE_PROJECT_GATEWAY,
        )
        self.assertTrue(consultation.worker_dispatch_requires_anchor)


class ParentToChildNoAnchorWorkIntakeScenarioTest(unittest.TestCase):
    """parent -> child work-intake is no-anchor; the CHILD owns the anchor decision.

    The Transition Command Matrix ``親 -> 子`` row and the #12748 Ticketless
    No-Anchor Work-Intake Primitive are explicit: the parent does NOT mint or
    require a Redmine anchor to hand the consultation to the child coordinator —
    the child creates/selects the anchor only once it sees the work shape. So the
    readiness gate must drive the real #12748 ``child-intake`` surface
    (:func:`resolve_child_intake_route` / :class:`TicketlessWorkIntake`), not a
    relative-route step whose anchor flag belongs to a different (worker-dispatch)
    layer. The child -> grandchild Redmine-anchor gate is a *separate* contract,
    pinned in :class:`ImplementationNeedsRedmineAnchorScenarioTest`.
    """

    def test_distinct_child_resolves_and_intake_is_no_anchor(self) -> None:
        # Two coordinator lanes: the parent's own (%parent) + a distinct child.
        # The child is resolved by identity (the other lane), and the intake route
        # never demands a Redmine anchor of itself.
        route = resolve_child_intake_route(
            [_candidate("%parent", lane_id="p"), _candidate("%child", lane_id="c")],
            repo_root=REPO,
            project_scope=PROJECT,
            caller_pane="%parent",
        )
        self.assertEqual(STATUS_CHILD_RESOLVED, route.status)
        self.assertTrue(route.ok)
        self.assertEqual("%child", route.selected.pane_id)
        self.assertFalse(route.anchor_required)

    def test_route_resolving_back_to_parent_is_same_lane_blocked(self) -> None:
        # The only coordinator-class lane is the caller's own: the child route
        # resolved back to the parent. Refuse to adopt the parent as its own child
        # (the self-fence), never a silent same-lane pick.
        route = resolve_child_intake_route(
            [_candidate("%parent", lane_id="p")],
            repo_root=REPO,
            project_scope=PROJECT,
            caller_pane="%parent",
        )
        self.assertEqual(STATUS_SAME_LANE, route.status)
        self.assertFalse(route.ok)
        self.assertIsNone(route.selected)
        self.assertFalse(route.anchor_required)

    def test_work_intake_payload_carries_the_no_anchor_invariants(self) -> None:
        # The forwarded work-intake fixes that the child owns the anchor decision,
        # the parent must not answer the domain itself, and worker dispatch still
        # needs a Redmine anchor — so a regression of the #12748 boundary is caught.
        intake = TicketlessWorkIntake(
            work_shape=WORK_SHAPE_DOMAIN_DESIGN,
            callback_to_role=ROLE_PROJECT_GATEWAY,
            callback_methods=(CALLBACK_VIA_TICKETLESS_CALLBACK,),
            read_contract=ROLE_CHILD_COORDINATOR,
        )
        self.assertTrue(intake.child_owns_anchor_decision)
        self.assertTrue(intake.parent_must_not_answer_domain)
        self.assertTrue(intake.worker_dispatch_requires_anchor)
        # The child coordinator — not the parent gateway — owns the anchor decision.
        self.assertEqual(ROLE_CHILD_COORDINATOR, intake.anchor_decision_owner)
        payload = intake.to_structured_dict()
        self.assertTrue(payload["child_owns_anchor_decision"])
        self.assertEqual(ROLE_CHILD_COORDINATOR, payload["anchor_decision_owner"])


class StandardPathNeedsNoPaneIdScenarioTest(unittest.TestCase):
    """The whole green path is expressible without %pane direct addressing."""

    def test_resolution_succeeds_across_separate_windows_without_a_session(self) -> None:
        # The normal department-root -> project-gateway path resolves across
        # separate windows/sessions: no session narrowing, no pane id — identity
        # is enough.
        gateway = _candidate("%30", session="project-window")
        resolution = resolve_project_gateway([gateway], _route(session=None))
        self.assertEqual(STATUS_FOUND, resolution.status)
        self.assertEqual(gateway, resolution.selected)

    def test_weak_role_is_never_auto_targeted_even_when_active(self) -> None:
        # A weak/ambiguous-role active pane is declined (the resolver never falls
        # back to "the active pane") — fail closed to gateway_missing.
        weak_active = _candidate(
            "%30", confidence=CONFIDENCE_WEAK, active=True
        )
        resolution = resolve_project_gateway([weak_active], _route())
        self.assertEqual(STATUS_GATEWAY_MISSING, resolution.status)

    def test_green_path_output_embeds_no_pane_id(self) -> None:
        # The route the grandparent acts on — adopt next-action and the missing
        # launch command — names semantic identity, never a %pane token.
        adopt_plan = resolve_relative_route(
            [_candidate("%30")],
            caller_role=ROLE_GRANDPARENT_COORDINATOR,
            repo_root=REPO,
            project_scope=PROJECT,
        )
        self.assertNotIn("%30", adopt_plan.next_action)
        launch_command = start_project_gateway_command(
            _route(), project_path=PROJECT_PATH
        )
        self.assertNotIn("%", launch_command)


if __name__ == "__main__":
    unittest.main()
