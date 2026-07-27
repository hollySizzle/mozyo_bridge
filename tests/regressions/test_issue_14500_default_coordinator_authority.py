"""Redmine #14500 — a bare ``mozyo`` default pair had no declarable coordinator authority.

The defect, exactly as observed (#14500 observed facts, reproduced in #14546 j#89697 / j#89736):
a fresh production bare ``mozyo`` default pair carries a startup self-attestation and an exact
cwd, yet literal ``mozyo-bridge workflow step`` fail-closes ``ambiguous_default_coordinator_role``
and stops with zero sends.

The trap in that failure is that it *looks* like a configuration mistake and is not. The durable
role authority (#13583) already existed, so the obvious reading was "declare the topology and step
again". But the declarable role vocabulary was ``grandparent_coordinator`` (department root, scope-
less, default lane) and ``project_gateway`` (whose lane id is **derived** to ``pgwv1_…`` and
therefore never matches the default lane). A single-workspace default pair is neither. There was no
declaration an operator could write that would resolve it — the fail-closed branch was reachable
from **every** configuration, so the authority was not merely unset, it was inexpressible.

This file pins that gap closed from both ends, and pins the fail-closed behaviour that must survive:

1. an **undeclared** default lane still fails closed ``ambiguous_default_coordinator_role`` —
   fixing the gap must not turn "no authority" into a guessed role (the original #13583 invariant:
   provider / pane / default placement are never promoted to a role authority);
2. a **declared** single-workspace ``coordinator`` resolves the default lane to a named role and a
   ONE-STEP managed-gateway forward — the transition #14500 required behaviour 2 asks for;
3. the authority is **provider-neutral**: it names a role, and the provider is cross-checked
   separately from ``provider_binding`` — a lane whose provider disagrees fails closed
   ``herdr_role_provider_mismatch`` instead of resolving on the surface it happens to run on;
4. **duplicate** authority on the default lane (a department root AND a single-workspace
   coordinator) is a typed parse failure, not a silent first-wins pick;
5. the coordinator forward is a **distinct** leg from the project-gateway forward — same target
   role, different sender — so the two can never share a fence generation or read as one another.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
    ROLE_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (  # noqa: E501
    ROLE_GRANDPARENT_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (  # noqa: E501
    FORWARD_COORDINATOR_TO_CHILD,
    FORWARD_GATEWAY_TO_CHILD,
    PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
    PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY,
    REASON_HERDR_FORWARD_MANAGED_GATEWAY_READY,
    SELECT_CHILD_WITH_SELF_FENCE,
    TICKETLESS_WORK_INTAKE,
    plan_forward_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    DEFAULT_LANE,
    REASON_ROLE_BINDING_INVALID,
    REASON_ROLE_PROVIDER_MISMATCH,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    STATUS_MISSING,
    STATUS_PROVIDER_MISMATCH,
    STATUS_RESOLVED,
    parse_role_bindings,
    resolve_role_for_lane,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
    EXECUTION_BLOCKED,
    EXECUTION_READY,
    OWNER_CHILD,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_LANE_UNRESOLVED,
    STATE_PARENT_WORK_INTAKE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (  # noqa: E501
    HERDR_PROVIDER_CLAUDE,
    HERDR_PROVIDER_CODEX,
    REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)

SCOPE = "bare_mozyo_workspace"


def _declaration(*entries) -> dict:
    return {"schema": SCHEMA_NAME, "version": SCHEMA_VERSION, "bindings": list(entries)}


def _coordinator_declaration(scope: str = SCOPE) -> dict:
    return _declaration({"role": ROLE_COORDINATOR, "project_scope": scope})


def _default_lane(provider: str = HERDR_PROVIDER_CODEX):
    """The bare `mozyo` default pair's classified lane (always the fail-closed classification)."""
    return classify_herdr_workflow_lane(
        provider=provider, lane_id=DEFAULT_LANE, repo_root="/repo", locator="w1:p1"
    )


def _resolve(declaration, *, provider: str = HERDR_PROVIDER_CODEX, expected: str = "codex"):
    parsed = parse_role_bindings(declaration)
    return resolve_role_for_lane(
        parsed, lane_id=DEFAULT_LANE, provider=provider, expected_provider=lambda _role: expected
    )


class UndeclaredDefaultLaneStillFailsClosedTest(unittest.TestCase):
    """Property 1: the fix must not turn "no authority" into a guessed role."""

    def test_absent_declaration_resolves_missing_and_step_fails_closed(self):
        resolution = _resolve(None)
        self.assertEqual(resolution.status, STATUS_MISSING)

        # A missing authority falls through to the classification, which fails closed.
        outcome = resolve_herdr_workflow_step(_default_lane(), role_authority=resolution)
        self.assertEqual(outcome.execution, EXECUTION_BLOCKED)
        self.assertEqual(outcome.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)
        self.assertEqual(outcome.state, STATE_LANE_UNRESOLVED)
        self.assertEqual(outcome.next_owner, OWNER_OPERATOR)
        self.assertEqual(outcome.primitive, PRIMITIVE_NONE)

    def test_empty_declaration_is_not_an_authority_either(self):
        outcome = resolve_herdr_workflow_step(
            _default_lane(), role_authority=_resolve(_declaration())
        )
        self.assertEqual(outcome.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)

    def test_default_lane_claude_is_not_an_implementation_worker(self):
        # The default-lane assistant must never be classified as a worker, declared or not.
        lane = _default_lane(provider=HERDR_PROVIDER_CLAUDE)
        self.assertIsNone(lane.caller_role)
        outcome = resolve_herdr_workflow_step(lane, role_authority=_resolve(None))
        self.assertEqual(outcome.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)


class DeclaredCoordinatorResolvesTheTransitionTest(unittest.TestCase):
    """Property 2: a declared single-workspace coordinator resolves the managed-gateway step."""

    def test_declared_coordinator_resolves_the_default_lane(self):
        resolution = _resolve(_coordinator_declaration())
        self.assertEqual(resolution.status, STATUS_RESOLVED)
        self.assertEqual(resolution.role, ROLE_COORDINATOR)
        self.assertEqual(resolution.lane_id, DEFAULT_LANE)
        self.assertEqual(resolution.project_scope, SCOPE)

    def test_step_names_the_one_step_managed_gateway_forward(self):
        outcome = resolve_herdr_workflow_step(
            _default_lane(), role_authority=_resolve(_coordinator_declaration())
        )
        self.assertEqual(outcome.execution, EXECUTION_READY)
        self.assertEqual(outcome.reason, REASON_HERDR_FORWARD_MANAGED_GATEWAY_READY)
        self.assertEqual(outcome.primitive, PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY)
        self.assertEqual(outcome.state, STATE_PARENT_WORK_INTAKE)
        self.assertEqual(outcome.next_owner, OWNER_CHILD)
        self.assertEqual(outcome.caller_role, ROLE_COORDINATOR)
        self.assertEqual(outcome.project_scope, SCOPE)
        # The coordinator mints no anchor: the managed gateway owns that decision.
        self.assertEqual(outcome.durable_anchor, "none")

    def test_the_resolved_step_is_no_longer_the_ambiguous_reason(self):
        # The exact observed token must be gone once the authority is declared — the whole
        # point of #14500. Asserted separately so a future refactor that reintroduces the
        # fail-closed branch under a declared authority fails HERE, not only on a shape check.
        outcome = resolve_herdr_workflow_step(
            _default_lane(), role_authority=_resolve(_coordinator_declaration())
        )
        self.assertNotEqual(outcome.reason, REASON_HERDR_DEFAULT_COORDINATOR_UNRESOLVED)


class ProviderNeutralityTest(unittest.TestCase):
    """Property 3: the authority names a ROLE; the provider is cross-checked separately."""

    def test_declaration_stores_no_provider(self):
        parsed = parse_role_bindings(_coordinator_declaration())
        self.assertTrue(parsed.ok)
        (binding,) = parsed.bindings
        # The value object has no provider field at all — a provider cannot be declared.
        self.assertFalse(hasattr(binding, "provider"))

    def test_rebinding_the_provider_resolves_the_same_role(self):
        # The same declaration resolves under a *different* bound provider: the role authority is
        # provider-neutral, and the provider follows provider_binding.
        resolution = _resolve(
            _coordinator_declaration(), provider="claude", expected="claude"
        )
        self.assertEqual(resolution.status, STATUS_RESOLVED)
        self.assertEqual(resolution.role, ROLE_COORDINATOR)

    def test_provider_disagreement_fails_closed(self):
        resolution = _resolve(
            _coordinator_declaration(), provider="claude", expected="codex"
        )
        self.assertEqual(resolution.status, STATUS_PROVIDER_MISMATCH)
        self.assertEqual(resolution.reason, REASON_ROLE_PROVIDER_MISMATCH)

        outcome = resolve_herdr_workflow_step(
            _default_lane(provider="claude"), role_authority=resolution
        )
        self.assertEqual(outcome.execution, EXECUTION_BLOCKED)
        self.assertEqual(outcome.reason, REASON_ROLE_PROVIDER_MISMATCH)
        self.assertEqual(outcome.next_owner, OWNER_OPERATOR)
        self.assertEqual(outcome.primitive, PRIMITIVE_NONE)

    def test_unresolvable_expected_provider_fails_closed(self):
        # A broken provider config cannot confirm the coordinator surface.
        parsed = parse_role_bindings(_coordinator_declaration())
        resolution = resolve_role_for_lane(
            parsed, lane_id=DEFAULT_LANE, provider="codex", expected_provider=lambda _r: None
        )
        self.assertEqual(resolution.status, STATUS_PROVIDER_MISMATCH)


class DuplicateAuthorityIsTypedBlockedTest(unittest.TestCase):
    """Property 4: two coordinator authorities on the default lane never resolve first-wins."""

    def test_grandparent_and_coordinator_collide_on_the_default_lane(self):
        parsed = parse_role_bindings(
            _declaration(
                {"role": ROLE_COORDINATOR, "project_scope": SCOPE},
                {"role": ROLE_GRANDPARENT_COORDINATOR},
            )
        )
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.reason, REASON_ROLE_BINDING_INVALID)
        self.assertIn(DEFAULT_LANE, parsed.detail)

    def test_collision_is_order_independent(self):
        parsed = parse_role_bindings(
            _declaration(
                {"role": ROLE_GRANDPARENT_COORDINATOR},
                {"role": ROLE_COORDINATOR, "project_scope": SCOPE},
            )
        )
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.reason, REASON_ROLE_BINDING_INVALID)

    def test_two_coordinators_collide_even_at_different_scopes(self):
        parsed = parse_role_bindings(
            _declaration(
                {"role": ROLE_COORDINATOR, "project_scope": "a"},
                {"role": ROLE_COORDINATOR, "project_scope": "b"},
            )
        )
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.reason, REASON_ROLE_BINDING_INVALID)

    def test_an_invalid_declaration_blocks_the_step_rather_than_falling_through(self):
        resolution = _resolve(
            _declaration(
                {"role": ROLE_COORDINATOR, "project_scope": SCOPE},
                {"role": ROLE_GRANDPARENT_COORDINATOR},
            )
        )
        self.assertTrue(resolution.blocked)
        outcome = resolve_herdr_workflow_step(_default_lane(), role_authority=resolution)
        self.assertEqual(outcome.execution, EXECUTION_BLOCKED)
        self.assertEqual(outcome.reason, REASON_ROLE_BINDING_INVALID)

    def test_coordinator_without_a_scope_is_invalid(self):
        parsed = parse_role_bindings(_declaration({"role": ROLE_COORDINATOR}))
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.reason, REASON_ROLE_BINDING_INVALID)


class CoordinatorLegIsDistinctFromTheGatewayLegTest(unittest.TestCase):
    """Property 5: same target role, different sender — never one conflated leg."""

    def test_coordinator_plans_a_managed_gateway_forward(self):
        plan = plan_forward_route(ROLE_COORDINATOR, SCOPE)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, FORWARD_COORDINATOR_TO_CHILD)
        self.assertEqual(plan.from_role, ROLE_COORDINATOR)
        self.assertEqual(plan.to_role, ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(plan.primitive, PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY)
        self.assertEqual(plan.ready_reason, REASON_HERDR_FORWARD_MANAGED_GATEWAY_READY)
        self.assertEqual(plan.select_mode, SELECT_CHILD_WITH_SELF_FENCE)
        self.assertEqual(plan.ticketless_kind, TICKETLESS_WORK_INTAKE)
        self.assertEqual(plan.project_scope, SCOPE)

    def test_the_two_work_intake_legs_never_share_an_identity(self):
        coordinator = plan_forward_route(ROLE_COORDINATOR, SCOPE)
        gateway = plan_forward_route("project_gateway", SCOPE)
        self.assertNotEqual(coordinator.direction, gateway.direction)
        self.assertNotEqual(coordinator.primitive, gateway.primitive)
        self.assertNotEqual(coordinator.ready_reason, gateway.ready_reason)
        self.assertNotEqual(coordinator.from_role, gateway.from_role)
        # The pre-existing gateway leg is unchanged (byte-invariant tokens).
        self.assertEqual(gateway.direction, FORWARD_GATEWAY_TO_CHILD)
        self.assertEqual(gateway.primitive, PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE)

    def test_the_fence_route_key_separates_the_two_legs(self):
        from mozyo_bridge.core.state.forward_outbox_fence import ForwardRouteKey

        coordinator = plan_forward_route(ROLE_COORDINATOR, SCOPE)
        gateway = plan_forward_route("project_gateway", SCOPE)
        keys = {
            ForwardRouteKey(
                workspace_id="ws",
                from_lane_id=DEFAULT_LANE,
                from_role=plan.from_role,
                to_role=plan.to_role,
                project_scope=plan.project_scope,
            )
            for plan in (coordinator, gateway)
        }
        # Two distinct route keys: one leg's active generation can never hold the other's.
        self.assertEqual(len(keys), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
