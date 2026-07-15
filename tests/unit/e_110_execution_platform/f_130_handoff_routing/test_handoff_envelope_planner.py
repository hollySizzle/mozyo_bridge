"""Unit tests for the handoff Anchor/Profile Envelope Planner (Redmine #13729 tranche 2).

Constructor-injected fake ports only (design j#78394 item 5) — no monkeypatch. The tests
pin:

- ``plan_anchor``: each ticketless branch + the Redmine/Asana anchor, plus the fail-closed
  ``EnvelopePlanError`` (``invalid_args`` for a malformed ticketless payload, ``invalid_anchor``
  for a bad anchor) with NO extra outcome fields (matching the early-stage inline block).
- ``plan_delivery_envelope``: the success envelope, and the cumulative partial-state extras
  each failing stage carries so the facade can reproduce a byte-identical blocked outcome.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_envelope_planner import (
    AnchorPlan,
    EnvelopePlanError,
    HandoffEnvelope,
    HandoffEnvelopePlanner,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AnchorError,
    TicketlessConsultationAnchor,
    TicketlessWorkIntakeAnchor,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS as CONSULT_CALLBACK_METHODS,
    CONSULTATION_PROJECT_DOMAIN,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    CALLBACK_METHODS as WORK_INTAKE_CALLBACK_METHODS,
    ROLE_DELEGATED_COORDINATOR,
    WORK_SHAPE_DOMAIN_DESIGN,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    TransitionRoleError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    WorkflowContractError,
)


class _Role:
    def __init__(self, resolved_text: str) -> None:
        self.resolved_text = resolved_text


class FakeOps:
    """Constructor-injectable fake :class:`EnvelopePlannerOps` with per-call scripting."""

    def __init__(self, **overrides) -> None:
        self.calls: list[tuple] = []
        self._overrides = overrides

    def _maybe_raise(self, name):
        exc = self._overrides.get(f"{name}_raises")
        if exc is not None:
            raise exc

    def normalize_anchor(self, source, *, task_id, comment_id, anchor_url, issue, journal):
        self.calls.append(("normalize_anchor", source, issue, journal))
        self._maybe_raise("normalize_anchor")
        return self._overrides.get("anchor", ("ANCHOR", source, issue))

    def build_execution_root(self, workdir_abs, *, repo_root_abs):
        self.calls.append(("build_execution_root", workdir_abs, repo_root_abs))
        return ("EXEC", workdir_abs, repo_root_abs)

    def infer_repo_root(self, cwd):
        self.calls.append(("infer_repo_root", cwd))
        return self._overrides.get("inferred_root", "/inferred")

    def resolve_handoff_profile_fields(self, role_profile, profile_field, human_pointer, repo_root):
        self.calls.append(("resolve_handoff_profile_fields", role_profile, human_pointer))
        self._maybe_raise("resolve_handoff_profile_fields")
        return {"role": role_profile}

    def resolve_role_profile(self, role_profile, profile_fields):
        self.calls.append(("resolve_role_profile", role_profile))
        self._maybe_raise("resolve_role_profile")
        return _Role(f"contract::{role_profile}")

    def resolve_transition_role(self, transition_role):
        self.calls.append(("resolve_transition_role", transition_role))
        self._maybe_raise("resolve_transition_role")
        return ("TRANSITION", transition_role)

    def resolve_workflow_contract(self, workflow_contract):
        self.calls.append(("resolve_workflow_contract", workflow_contract))
        self._maybe_raise("resolve_workflow_contract")
        return ("CONTRACT", workflow_contract)

    def build_notification_body(self, anchor, kind, summary, receiver, **kw):
        self.calls.append(("build_notification_body", kind, receiver, kw))
        self._maybe_raise("build_notification_body")
        return f"BODY::{kind}::{receiver}"

    def build_marker(self, anchor, kind, receiver):
        self.calls.append(("build_marker", kind, receiver))
        return f"MARKER::{kind}::{receiver}"


def _inp(**kw) -> HandoffCommandInput:
    return HandoffCommandInput(**kw)


class _Anchorish:
    def human_pointer(self):
        return "#9 j#9"


class PlanAnchorTest(unittest.TestCase):
    def test_redmine_anchor_success(self) -> None:
        ops = FakeOps(anchor="RM")
        plan = HandoffEnvelopePlanner(ops).plan_anchor(
            _inp(source="redmine", issue="9", journal="9")
        )
        self.assertIsInstance(plan, AnchorPlan)
        self.assertEqual(plan.anchor, "RM")
        self.assertIsNone(plan.callback_payload)
        self.assertIn(("normalize_anchor", "redmine", "9", "9"), ops.calls)

    def test_bad_anchor_raises_invalid_anchor_with_no_extras(self) -> None:
        ops = FakeOps(normalize_anchor_raises=AnchorError("bad anchor"))
        with self.assertRaises(EnvelopePlanError) as ctx:
            HandoffEnvelopePlanner(ops).plan_anchor(_inp(source="redmine"))
        exc = ctx.exception
        self.assertEqual(exc.reason, "invalid_anchor")
        self.assertEqual(exc.message, "bad anchor")
        self.assertEqual(exc.outcome_extra, {})
        self.assertEqual(exc.emit_extra, {})

    def test_ticketless_callback_success(self) -> None:
        plan = HandoffEnvelopePlanner(FakeOps()).plan_anchor(
            _inp(
                ticketless=True,
                classification="no_dispatch",
                dispatch_decision="hand_back_to_caller",
                workflow_next_owner="caller",
                callback_reason="no_dispatch_decided",
                read_contract="grandparent_coordinator",
            )
        )
        self.assertIsNotNone(plan.callback_payload)
        self.assertIsNone(plan.consultation_payload)
        self.assertIsNone(plan.work_intake_payload)

    def test_ticketless_bad_payload_raises_invalid_args(self) -> None:
        with self.assertRaises(EnvelopePlanError) as ctx:
            HandoffEnvelopePlanner(FakeOps()).plan_anchor(
                _inp(ticketless=True, classification="not-a-real-classification")
            )
        self.assertEqual(ctx.exception.reason, "invalid_args")
        self.assertEqual(ctx.exception.outcome_extra, {})

    # -- Asana: same non-ticketless branch as Redmine, delegating to the port ----
    def test_asana_anchor_delegates_to_normalize_anchor_port(self) -> None:
        ops = FakeOps(anchor="ASANA")
        plan = HandoffEnvelopePlanner(ops).plan_anchor(
            _inp(source="asana", task_id="T1", comment_id="C1")
        )
        self.assertEqual(plan.anchor, "ASANA")
        self.assertIsNone(plan.callback_payload)
        # the anchored branch DOES call the normalize_anchor port, with source=asana
        self.assertIn(("normalize_anchor", "asana", None, None), ops.calls)

    # -- ticketless consultation --------------------------------------------------
    def test_ticketless_consultation_success(self) -> None:
        ops = FakeOps()
        plan = HandoffEnvelopePlanner(ops).plan_anchor(
            _inp(
                ticketless=True,
                ticketless_consultation=True,
                consultation_kind=CONSULTATION_PROJECT_DOMAIN,
                callback_to_role="grandparent_coordinator",
                callback_methods=tuple(CONSULT_CALLBACK_METHODS),
                read_contract="project_gateway",
            )
        )
        self.assertIsNotNone(plan.consultation_payload)
        self.assertIsNone(plan.callback_payload)
        self.assertIsNone(plan.work_intake_payload)
        self.assertIsInstance(plan.anchor, TicketlessConsultationAnchor)
        # ticketless branch must NOT touch the normalize_anchor port
        self.assertFalse(any(c[0] == "normalize_anchor" for c in ops.calls))

    def test_ticketless_consultation_invalid_raises_without_port_call(self) -> None:
        ops = FakeOps()
        with self.assertRaises(EnvelopePlanError) as ctx:
            HandoffEnvelopePlanner(ops).plan_anchor(
                _inp(
                    ticketless=True,
                    ticketless_consultation=True,
                    consultation_kind="not-a-real-consultation",
                    callback_to_role="grandparent_coordinator",
                    callback_methods=tuple(CONSULT_CALLBACK_METHODS),
                    read_contract="project_gateway",
                )
            )
        self.assertEqual(ctx.exception.reason, "invalid_args")
        self.assertEqual(ctx.exception.outcome_extra, {})
        self.assertFalse(any(c[0] == "normalize_anchor" for c in ops.calls))

    # -- ticketless work-intake ---------------------------------------------------
    def test_ticketless_work_intake_success(self) -> None:
        ops = FakeOps()
        plan = HandoffEnvelopePlanner(ops).plan_anchor(
            _inp(
                ticketless=True,
                ticketless_work_intake=True,
                work_shape=WORK_SHAPE_DOMAIN_DESIGN,
                callback_to_role=ROLE_PROJECT_GATEWAY,
                callback_methods=tuple(WORK_INTAKE_CALLBACK_METHODS),
                read_contract=ROLE_DELEGATED_COORDINATOR,
            )
        )
        self.assertIsNotNone(plan.work_intake_payload)
        self.assertIsNone(plan.callback_payload)
        self.assertIsNone(plan.consultation_payload)
        self.assertIsInstance(plan.anchor, TicketlessWorkIntakeAnchor)
        self.assertFalse(any(c[0] == "normalize_anchor" for c in ops.calls))

    def test_ticketless_work_intake_invalid_raises_without_port_call(self) -> None:
        ops = FakeOps()
        with self.assertRaises(EnvelopePlanError) as ctx:
            HandoffEnvelopePlanner(ops).plan_anchor(
                _inp(
                    ticketless=True,
                    ticketless_work_intake=True,
                    work_shape="not-a-real-work-shape",
                    callback_to_role=ROLE_PROJECT_GATEWAY,
                    callback_methods=tuple(WORK_INTAKE_CALLBACK_METHODS),
                    read_contract=ROLE_DELEGATED_COORDINATOR,
                )
            )
        self.assertEqual(ctx.exception.reason, "invalid_args")
        self.assertEqual(ctx.exception.outcome_extra, {})
        self.assertFalse(any(c[0] == "normalize_anchor" for c in ops.calls))


class PlanDeliveryEnvelopeTest(unittest.TestCase):
    def _plan(self, ops, inp, **kw):
        base = dict(
            anchor=_Anchorish(),
            callback_payload=None,
            consultation_payload=None,
            work_intake_payload=None,
            repo_root=Path("/repo"),
            resolved_target_repo=None,
            target_cwd="/cwd",
            summary="s",
            receiver="claude",
            kind="reply",
        )
        base.update(kw)
        return HandoffEnvelopePlanner(ops).plan_delivery_envelope(inp, **base)

    def test_minimal_envelope(self) -> None:
        env = self._plan(FakeOps(), _inp())
        self.assertIsInstance(env, HandoffEnvelope)
        self.assertIsNone(env.execution_root)
        self.assertIsNone(env.role_profile_resolution)
        self.assertIsNone(env.role_profile_contract)
        self.assertEqual(env.body, "BODY::reply::claude")
        self.assertEqual(env.marker, "MARKER::reply::claude")

    def test_execution_root_uses_explicit_target_repo(self) -> None:
        ops = FakeOps()
        env = self._plan(
            ops, _inp(workdir="/w"), resolved_target_repo="/target-repo"
        )
        self.assertEqual(env.execution_root[0], "EXEC")
        # infer_repo_root NOT called when an explicit target repo is present
        self.assertFalse(any(c[0] == "infer_repo_root" for c in ops.calls))

    def test_execution_root_falls_back_to_infer(self) -> None:
        ops = FakeOps(inferred_root="/inferred")
        self._plan(ops, _inp(workdir="/w"), resolved_target_repo=None)
        self.assertIn(("infer_repo_root", "/cwd"), ops.calls)

    def test_role_profile_resolved_and_contract_carried(self) -> None:
        env = self._plan(FakeOps(), _inp(role_profile="implementation_worker"))
        self.assertEqual(env.role_profile_contract, "contract::implementation_worker")

    def test_role_profile_error_carries_only_execution_root(self) -> None:
        ops = FakeOps(resolve_role_profile_raises=RoleProfileError("bad role"))
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan(ops, _inp(workdir="/w", role_profile="x"), resolved_target_repo="/r")
        exc = ctx.exception
        self.assertEqual(exc.reason, "invalid_args")
        self.assertEqual(set(exc.outcome_extra), {"execution_root"})
        self.assertEqual(exc.emit_extra, {})

    def test_transition_error_carries_execution_root_and_role_profile(self) -> None:
        ops = FakeOps(resolve_transition_role_raises=TransitionRoleError("bad tr"))
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan(ops, _inp(role_profile="r", transition_role="t"))
        exc = ctx.exception
        self.assertEqual(set(exc.outcome_extra), {"execution_root", "role_profile"})
        self.assertEqual(set(exc.emit_extra), {"role_profile_contract"})
        self.assertEqual(exc.emit_extra["role_profile_contract"], "contract::r")

    def test_workflow_contract_error_carries_cumulative_state(self) -> None:
        ops = FakeOps(resolve_workflow_contract_raises=WorkflowContractError("bad wc"))
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan(
                ops, _inp(role_profile="r", transition_role="t", workflow_contract="w")
            )
        exc = ctx.exception
        self.assertEqual(
            set(exc.outcome_extra), {"execution_root", "role_profile", "transition_role"}
        )
        self.assertEqual(set(exc.emit_extra), {"role_profile_contract"})

    def test_body_error_carries_full_state_and_payloads(self) -> None:
        ops = FakeOps(build_notification_body_raises=AnchorError("bad body"))
        with self.assertRaises(EnvelopePlanError) as ctx:
            self._plan(ops, _inp(), callback_payload="CB")
        exc = ctx.exception
        self.assertEqual(exc.reason, "invalid_args")
        self.assertEqual(
            set(exc.outcome_extra),
            {
                "execution_root",
                "role_profile",
                "transition_role",
                "workflow_contract",
                "ticketless_callback",
                "ticketless_consultation",
                "ticketless_work_intake",
            },
        )
        self.assertEqual(exc.outcome_extra["ticketless_callback"], "CB")


if __name__ == "__main__":
    unittest.main()
