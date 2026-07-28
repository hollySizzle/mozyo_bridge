"""``workflow step`` executable-forward-leg wiring tests (Redmine #14546, review j#90032 F1).

The coordinator leg was resolvable and unfirable. The pure resolver returned ``execution=ready``
with ``primitive=herdr_forward_managed_gateway``, but the CLI's executable-leg classifier listed the
two pre-existing forward tokens by hand, so a non-dry-run ``workflow step`` fell through every
execute branch and exited 0 having sent nothing. The single acceptance the coordinator leg exists to
satisfy — the default coordinator's one-step transition to its managed gateway — was therefore
unreachable in the product while every pure test passed.

The tests here are at the level the defect lived at: the classifier and the top-level command, not
the resolver. They also pin the *shape* of the fix, because a hand-listed tuple is what failed:
every primitive the route matrix can plan must be admitted, so adding a direction can never again
leave the executor behind.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (  # noqa: E501
    FORWARD_PRIMITIVES,
    FORWARD_ROLES,
    PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
    PRIMITIVE_HERDR_FORWARD_CONSULT,
    PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY,
    plan_forward_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
    EXECUTION_BLOCKED,
    EXECUTION_NO_OP,
    EXECUTION_READY,
    OWNER_CHILD,
    PRIMITIVE_NONE,
    STATE_PARENT_WORK_INTAKE,
    WorkflowStepOutcome,
)


def _outcome(primitive: str, execution: str = EXECUTION_READY) -> WorkflowStepOutcome:
    return WorkflowStepOutcome(
        state=STATE_PARENT_WORK_INTAKE,
        next_action="forward",
        execution=execution,
        reason="r",
        next_owner=OWNER_CHILD,
        primitive=primitive,
        durable_anchor="none",
    )


class ForwardLegClassifierTest(unittest.TestCase):
    def test_every_planned_primitive_is_an_executable_leg(self):
        # The coherence property the hand-listed tuple lacked: whatever the matrix can plan, the
        # classifier admits. A new direction cannot silently become unfirable.
        for role in FORWARD_ROLES:
            plan = plan_forward_route(role, "scope")
            self.assertIsNotNone(plan, role)
            self.assertIn(plan.primitive, FORWARD_PRIMITIVES, role)
            self.assertTrue(
                cli_workflow._is_herdr_forward_leg(_outcome(plan.primitive)), role
            )

    def test_the_managed_gateway_leg_is_executable(self):
        self.assertTrue(
            cli_workflow._is_herdr_forward_leg(
                _outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY)
            )
        )

    def test_the_pre_existing_legs_stay_executable(self):
        for primitive in (
            PRIMITIVE_HERDR_FORWARD_CONSULT,
            PRIMITIVE_HERDR_FORWARD_CHILD_INTAKE,
        ):
            self.assertTrue(cli_workflow._is_herdr_forward_leg(_outcome(primitive)), primitive)

    def test_a_non_ready_outcome_is_never_an_executable_leg(self):
        for execution in (EXECUTION_BLOCKED, EXECUTION_NO_OP):
            self.assertFalse(
                cli_workflow._is_herdr_forward_leg(
                    _outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY, execution=execution)
                ),
                execution,
            )

    def test_an_unrelated_primitive_is_never_an_executable_leg(self):
        for primitive in (PRIMITIVE_NONE, "handoff_send", "herdr_dispatch_worker"):
            self.assertFalse(cli_workflow._is_herdr_forward_leg(_outcome(primitive)), primitive)

    def test_the_forward_legs_stay_out_of_the_generic_executable_set(self):
        # They ride a dedicated fence and executor; the generic set is the tmux primitive rail.
        for primitive in FORWARD_PRIMITIVES:
            self.assertFalse(_outcome(primitive).executable, primitive)


class WorkflowStepFiresTheManagedGatewayLegTest(unittest.TestCase):
    """The top-level command, which is where the miss actually showed."""

    def setUp(self) -> None:
        import tempfile

        self._store = tempfile.TemporaryDirectory()
        self.addCleanup(self._store.cleanup)
        self.calls: list = []
        self._patch(
            "_execute_herdr_forward_leg",
            lambda outcome, args: (self.calls.append(outcome.primitive) or (0, "sent")),
        )

    def _patch(self, name, value):
        original = getattr(cli_workflow, name)
        setattr(cli_workflow, name, value)
        self.addCleanup(setattr, cli_workflow, name, original)

    def _resolve_as(self, outcome):
        # `_herdr_step_preflight` is the seam the command resolves the herdr-native outcome
        # through; patching it drives the real command body, which is where the miss lived.
        # The two precedence rules that legitimately override any outcome — the durable operator
        # startup gate and the gateway disposition intake — are neutralised so this test measures
        # the forward-leg wiring and nothing else. Each has its own tests.
        self._patch("_herdr_step_preflight", lambda _args: outcome)
        self._patch("_maybe_operator_startup_resume_outcome", lambda _args, _outcome: None)

    def _run(self, *, dry_run):
        # An empty store path isolates the run from the operator's home runtime store, whose
        # pending gating action would legitimately downgrade any forward leg to `blocked`
        # (`store_pending_action_gates`). That rule has its own tests; this one is about wiring.
        args = argparse.Namespace(
            repo=str(ROOT), dry_run=dry_run, as_json=True, session=None,
            issue=None, journal=None, callback=None,
            store_path=str(Path(self._store.name) / "absent-workflow-store.sqlite"),
        )
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_workflow.cmd_workflow_step(args)
        return rc, buf.getvalue()

    def test_a_non_dry_run_fires_the_leg_exactly_once(self):
        self._resolve_as(_outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY))
        rc, _out = self._run(dry_run=False)
        self.assertEqual(self.calls, [PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY])
        self.assertEqual(rc, 0)

    def test_a_dry_run_fires_nothing(self):
        self._resolve_as(_outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY))
        self._run(dry_run=True)
        self.assertEqual(self.calls, [])

    def test_a_failing_leg_propagates_its_exit_code(self):
        self._resolve_as(_outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY))
        self._patch(
            "_execute_herdr_forward_leg",
            lambda outcome, args: (self.calls.append(outcome.primitive) or (3, "zero-send")),
        )
        rc, _out = self._run(dry_run=False)
        self.assertEqual(self.calls, [PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY])
        self.assertEqual(rc, 3)

    def test_a_blocked_coordinator_outcome_fires_nothing(self):
        self._resolve_as(
            _outcome(PRIMITIVE_HERDR_FORWARD_MANAGED_GATEWAY, execution=EXECUTION_BLOCKED)
        )
        self._run(dry_run=False)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
