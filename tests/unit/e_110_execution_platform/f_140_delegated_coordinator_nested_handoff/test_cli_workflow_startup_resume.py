"""Unit tests for the ``workflow step`` startup-resume wiring (Redmine #13813 F2, j#79332).

The resume leg is reached through ``mozyo-bridge workflow step`` (no new CLI): a durable gate
awaiting resume routes the step to :data:`PRIMITIVE_OPERATOR_STARTUP_RESUME`. These tests pin
the emitter (``_maybe_operator_startup_resume_outcome`` — only ``operator_reported_done``
overrides the outcome, fail-soft otherwise), the predicate, and the executor delegation.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    cli_workflow,
    operator_startup_resume_leg,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (  # noqa: E402
    GATE_READ_CORRUPT,
    GATE_READ_GATE,
    GATE_READ_NONE,
    LatestGateRead,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    GateApproval,
    GateClassification,
    GateTarget,
    OriginalRequest,
    build_required_gate,
    repo_identity_digest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate_lattice import (  # noqa: E402
    approve_gate,
    report_operator_done,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E402
    EXECUTION_READY,
    PRIMITIVE_NONE,
    PRIMITIVE_OPERATOR_STARTUP_RESUME,
    WorkflowStepOutcome,
)


def _target() -> GateTarget:
    return GateTarget(
        workspace_id="ws",
        repo_identity_digest=repo_identity_digest("r"),
        execution_root=".",
        lane_id="lane",
        target_role="implementation_worker",
        target_assigned_name="worker-a",
        provider_id="claude",
        runtime_role="claude",
        agent_generation=3,
        lane_revision=1,
    )


def _required():
    return build_required_gate(
        gate_id="g1",
        action_generation=1,
        original_request=OriginalRequest(
            source="redmine", issue="13760", journal="77948", delivery_id="deliv-1"
        ),
        target=_target(),
        classification=GateClassification(
            blocker_id="first_run_theme", profile_version="2", classifier_version="1", observed_at="x"
        ),
    )


def _done():
    return report_operator_done(approve_gate(_required(), approval=GateApproval(source_journal="78412")))


def _outcome(**overrides) -> WorkflowStepOutcome:
    kwargs = dict(
        state="child_worker",
        next_action="monitor",
        execution="ready",
        reason="none",
        next_owner="child",
        primitive=PRIMITIVE_NONE,
        durable_anchor="redmine:issue=13760:journal=77948",
    )
    kwargs.update(overrides)
    return WorkflowStepOutcome(**kwargs)


class _FakeSource:
    def __init__(self, read):
        self._read = read

    def __call__(self, issue):
        return self._read


class MaybeResumeOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = operator_startup_resume_leg._default_gate_source
        self.addCleanup(setattr, operator_startup_resume_leg, "_default_gate_source", self._orig)

    def _patch_gate(self, read: LatestGateRead) -> None:
        operator_startup_resume_leg._default_gate_source = lambda repo_root, env: _FakeSource(read)

    def test_operator_reported_done_routes_to_resume(self) -> None:
        self._patch_gate(LatestGateRead(GATE_READ_GATE, _done()))
        result = cli_workflow._maybe_operator_startup_resume_outcome(
            argparse.Namespace(), _outcome()
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.primitive, PRIMITIVE_OPERATOR_STARTUP_RESUME)
        self.assertEqual(result.execution, EXECUTION_READY)
        self.assertTrue(cli_workflow._is_startup_resume_leg(result))

    def test_pre_clear_gate_does_not_route(self) -> None:
        # A `required` gate is pre-clear: no resume (leaves the normal outcome).
        self._patch_gate(LatestGateRead(GATE_READ_GATE, _required()))
        self.assertIsNone(
            cli_workflow._maybe_operator_startup_resume_outcome(argparse.Namespace(), _outcome())
        )

    def test_no_gate_does_not_route(self) -> None:
        self._patch_gate(LatestGateRead(GATE_READ_NONE))
        self.assertIsNone(
            cli_workflow._maybe_operator_startup_resume_outcome(argparse.Namespace(), _outcome())
        )

    def test_corrupt_gate_does_not_route(self) -> None:
        self._patch_gate(LatestGateRead(GATE_READ_CORRUPT))
        self.assertIsNone(
            cli_workflow._maybe_operator_startup_resume_outcome(argparse.Namespace(), _outcome())
        )

    def test_unreadable_source_is_fail_soft(self) -> None:
        def _raises(repo_root, env):
            def _src(issue):
                raise RuntimeError("ticket provider down")

            return _src

        operator_startup_resume_leg._default_gate_source = _raises
        self.assertIsNone(
            cli_workflow._maybe_operator_startup_resume_outcome(argparse.Namespace(), _outcome())
        )

    def test_no_issue_anywhere_does_not_route(self) -> None:
        # No anchor issue and no --issue arg: nothing to read, no resume.
        self.assertIsNone(
            cli_workflow._maybe_operator_startup_resume_outcome(
                argparse.Namespace(), _outcome(durable_anchor="none")
            )
        )


class ResumeLegPredicateAndExecutorTests(unittest.TestCase):
    def test_predicate_requires_primitive_and_ready(self) -> None:
        self.assertFalse(cli_workflow._is_startup_resume_leg(_outcome()))
        self.assertTrue(
            cli_workflow._is_startup_resume_leg(
                _outcome(primitive=PRIMITIVE_OPERATOR_STARTUP_RESUME, execution=EXECUTION_READY)
            )
        )
        # Not ready -> not the leg.
        self.assertFalse(
            cli_workflow._is_startup_resume_leg(
                _outcome(primitive=PRIMITIVE_OPERATOR_STARTUP_RESUME, execution="dry_run")
            )
        )

    def _run_executor(self, **result_fields):
        fields = dict(
            result="resume_delivered",
            fence_state="delivered",
            sent=True,
            record_failed=False,
            needs_reconcile=False,
            detail="ok",
            ok=True,
        )
        fields.update(result_fields)
        orig = operator_startup_resume_leg.execute_startup_resume
        operator_startup_resume_leg.execute_startup_resume = lambda args, issue: type("R", (), fields)()
        try:
            return cli_workflow._execute_startup_resume_leg(
                _outcome(primitive=PRIMITIVE_OPERATOR_STARTUP_RESUME, execution=EXECUTION_READY),
                argparse.Namespace(),
            )
        finally:
            operator_startup_resume_leg.execute_startup_resume = orig

    def test_clean_delivery_is_rc_zero(self) -> None:
        rc, text = self._run_executor()
        self.assertEqual(rc, 0)
        self.assertIn("resume_delivered", text)

    def test_record_failed_delivery_is_rc_one(self) -> None:
        # review j#79366 F2: a record_failed / needs_reconcile delivery must surface rc=1 even
        # though the delivery itself was positive (operator reconcile required).
        rc, text = self._run_executor(record_failed=True, needs_reconcile=True)
        self.assertEqual(rc, 1)
        self.assertIn("record_failed: True", text)

    def test_needs_reconcile_uncertain_is_rc_one(self) -> None:
        rc, _ = self._run_executor(
            result="resume_uncertain", fence_state="uncertain", needs_reconcile=True, ok=False
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
