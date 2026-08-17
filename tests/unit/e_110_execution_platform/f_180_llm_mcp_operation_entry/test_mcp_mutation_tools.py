"""Mutating tool handlers + the mutating-declaration guard (Redmine #15152).

Two claims, each pinned structurally:

1. **Projection discipline.** The mutating handlers republish closed tokens and
   caller-supplied identities ONLY. Producer free text (the CLI ``die``
   message, an outcome's ``next_action`` prose) and pane / path evidence
   (``target``, ``worktree_path``, ``gateway_pane``, ``steps``) are dropped —
   the #15151 r4f3 / r5f1 allowlist discipline, applied to the mutating
   surface.
2. **The guard still guards.** ``read_only=False`` is publishable only for the
   closed ``MUTATING_TOOL_NAMES`` declaration, the declaration must not claim
   read-only, and the forbidden-token check applies to a mutating tool's input
   schema exactly as it does to a read tool's.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.mutation_tools import (  # noqa: E402,E501
    HANDOFF_REFUSAL_SENTENCE,
    run_handoff_send,
    run_sublane_start_tool,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.tool_catalog import (  # noqa: E402,E501
    MUTATING_TOOL_NAMES,
    TOOL_CATALOG,
    ToolDefinition,
    catalog_surface_violations,
)


def _context() -> ReadPlanContext:
    return ReadPlanContext(repo_root=Path("/nonexistent-repo"))


def _delivery_outcome(**overrides):
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
        DeliveryOutcome,
    )

    fields = dict(
        status="blocked",
        reason="invalid_anchor",
        receiver="codex",
        target="%7",  # pane evidence: must never be republished
        source="redmine",
        anchor={"source": "redmine", "issue": "15152", "journal": "1"},
        mode="queue-enter",
        kind="reply",
        next_action_owner="sender",
        next_action="repair the anchor then re-run from pane %7",  # producer prose
        notification_marker=None,
    )
    fields.update(overrides)
    return DeliveryOutcome(**fields)


class HandoffProjectionTests(unittest.TestCase):
    def _run_with_result(self, result):
        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service as api  # noqa: E501

        with patch.object(api, "run_handoff", return_value=result):
            return run_handoff_send({"to": "codex", "issue": "1", "journal": "2"}, _context())

    def _fail_closed_result(self, outcome):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        return HandoffResult(
            operation="send",
            status="fail_closed",
            exit_code=2,
            outcome=outcome,
            delivered=False,
            error_message="died: pane %7 at /home/holly/private/checkout is busy",
        )

    def test_the_gate_message_is_never_forwarded(self) -> None:
        outcome = self._run_with_result(self._fail_closed_result(_delivery_outcome()))

        body = json.dumps(outcome.payload)
        self.assertTrue(outcome.is_error)
        self.assertNotIn("/home/holly/private", body)
        self.assertNotIn("%7", body)
        self.assertEqual(HANDOFF_REFUSAL_SENTENCE, outcome.payload["refusal"])

    def test_the_outcome_projection_is_the_allowlist(self) -> None:
        outcome = self._run_with_result(self._fail_closed_result(_delivery_outcome()))

        projected = outcome.payload["outcome"]
        # Closed tokens and the caller-supplied anchor survive...
        self.assertEqual("blocked", projected["status"])
        self.assertEqual("invalid_anchor", projected["reason"])
        self.assertEqual("codex", projected["receiver"])
        self.assertEqual(
            {"source": "redmine", "issue": "15152", "journal": "1"},
            projected["anchor"],
        )
        # ...pane evidence and producer prose do not.
        self.assertNotIn("target", projected)
        self.assertNotIn("next_action", projected)
        self.assertNotIn("execution_root", projected)

    def test_a_delivered_completion_reports_delivered_true(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffResult,
        )

        outcome = self._run_with_result(
            HandoffResult(
                operation="send",
                status="completed",
                exit_code=0,
                outcome=_delivery_outcome(status="sent", reason="ok"),
                delivered=True,
            )
        )

        self.assertFalse(outcome.is_error)
        self.assertTrue(outcome.payload["delivered"])
        self.assertEqual("", outcome.payload["refusal"])


class SublaneProjectionTests(unittest.TestCase):
    def _actuation_outcome(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            ActuationStep,
            SublaneActuationOutcome,
        )

        return SublaneActuationOutcome(
            status="ready",
            execute=False,
            reason="plan",
            issue="15152",
            lane_label="issue_15152_probe",
            branch="issue_15152_probe",
            worktree_path="/home/holly/private/.worktrees/issue_15152_probe",
            gateway_pane="%3",
            worker_pane="%4",
            dispatch_target="%3",
            steps=(
                ActuationStep(
                    order=1,
                    title="worktree",
                    status="ready",
                    detail="would run",
                    command="git worktree add /home/holly/private/.worktrees/x b",
                ),
            ),
        )

    def test_the_outcome_projection_drops_panes_paths_and_steps(self) -> None:
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501

        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=0, outcome=self._actuation_outcome()
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe"}, _context()
            )

        body = json.dumps(outcome.payload)
        self.assertNotIn("/home/holly/private", body)
        self.assertNotIn("%3", body)
        self.assertNotIn("git worktree add", body)
        projected = outcome.payload["outcome"]
        self.assertEqual("ready", projected["status"])
        self.assertEqual("issue_15152_probe", projected["lane_label"])
        self.assertNotIn("worktree_path", projected)
        self.assertNotIn("gateway_pane", projected)
        self.assertNotIn("steps", projected)

    def test_a_hostile_actuation_reason_is_reconstructed_not_leaked(self) -> None:
        # #15152 R4 (review j#106903 finding_reasonproseleak): the ACTUATION
        # producer's `reason` concatenates a gate's free-text detail (a sender
        # preflight can emit "workspace anchor unreadable (/home/holly/private/
        # ...)"), so the raw reason must never reach the public surface — neither
        # structuredContent nor the text summary. The public reason is
        # reconstructed from the closed status + blocked_reasons tokens.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        hostile = SublaneActuationOutcome(
            status="blocked",
            execute=True,
            reason=(
                "dispatch sender attestation failed before actuation; "
                "workspace anchor unreadable (/home/holly/private/secret at %5)"
            ),
            issue="15152",
            lane_label="issue_15152_probe",
            blocked_reasons=("missing_identity", "sender_attestation"),
        )
        result = svc.SublaneStartResult(
            status=svc.STATUS_COMPLETED, exit_code=1, outcome=hostile
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "15152", "lane_label": "issue_15152_probe", "actuate": True},
                _context(),
            )

        # Neither the structured payload nor the text summary carries the raw
        # reason's private path / pane id.
        body = json.dumps(outcome.payload)
        self.assertNotIn("/home/holly/private", body)
        self.assertNotIn("%5", body)
        self.assertNotIn("/home/holly/private", outcome.summary)
        self.assertNotIn("%5", outcome.summary)
        # The public reason is reconstructed from the closed tokens.
        projected_reason = outcome.payload["outcome"]["reason"]
        self.assertIn("blocked", projected_reason)
        self.assertIn("sender_attestation", projected_reason)
        self.assertNotIn("unreadable", projected_reason)

    def test_a_service_refusal_is_reconstructed_not_forwarded(self) -> None:
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E501

        result = svc.SublaneStartResult(
            status=svc.STATUS_REFUSED,
            exit_code=1,
            refusal=svc.SublaneStartRefusal(
                reason="parent_gateway_binding_missing",
                message="producer prose mentioning /home/holly/private and %5",
            ),
        )
        with patch.object(svc, "run_sublane_start", return_value=result):
            outcome = run_sublane_start_tool(
                {"issue": "1", "lane_label": "x", "lane_kind": "delegated_coordinator"},
                _context(),
            )

        self.assertTrue(outcome.is_error)
        self.assertEqual(
            "parent_gateway_binding_missing", outcome.payload["refusal_reason"]
        )
        body = json.dumps(outcome.payload)
        self.assertNotIn("/home/holly/private", body)
        self.assertNotIn("%5", body)
        # The fixed sentence names the recovery surface, not the producer text.
        self.assertIn("declare-project-gateway", outcome.payload["refusal"])


class MutatingDeclarationGuardTests(unittest.TestCase):
    def _tool(self, name: str, schema: dict, *, read_only: bool) -> dict:
        return {
            name: ToolDefinition(
                name=name,
                title=name,
                description="synthetic",
                input_schema=schema,
                output_schema={"type": "object"},
                read_only=read_only,
            )
        }

    def test_an_undeclared_mutating_tool_is_caught(self) -> None:
        violations = catalog_surface_violations(
            self._tool("probe", {"type": "object"}, read_only=False)
        )
        self.assertTrue(any("undeclared mutating" in v for v in violations))

    def test_a_declared_mutating_tool_claiming_read_only_is_caught(self) -> None:
        violations = catalog_surface_violations(
            self._tool("handoff_send", {"type": "object"}, read_only=True)
        )
        self.assertTrue(any("claims to be read-only" in v for v in violations))

    def test_the_token_guard_applies_to_declared_mutating_tools_too(self) -> None:
        violations = catalog_surface_violations(
            self._tool(
                "probe",
                {"type": "object", "properties": {"target_pane": {"type": "string"}}},
                read_only=False,
            ),
            mutating_names=frozenset({"probe"}),
        )
        self.assertTrue(any("pane" in v for v in violations))

    def test_no_shipped_mutating_input_can_name_a_pane_or_command(self) -> None:
        # Belt-and-suspenders over the guard: the shipped mutating schemas
        # carry no property that could address a raw pane, tmux target, or
        # command string — the structural reason an unmanaged (identity-less)
        # row cannot be reached from this surface (#15152 j#102930).
        for name in MUTATING_TOOL_NAMES:
            properties = TOOL_CATALOG[name].input_schema["properties"]
            for prop in properties:
                for token in ("pane", "tmux", "command", "argv", "target_session"):
                    self.assertNotIn(token, prop, f"{name}.{prop}")
            self.assertNotIn("target", properties)  # a bare pane locator field


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
