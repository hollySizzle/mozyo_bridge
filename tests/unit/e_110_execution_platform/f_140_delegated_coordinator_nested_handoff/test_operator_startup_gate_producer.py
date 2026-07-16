"""Unit tests for the authoritative v3 gate producer (Redmine #13813 review j#79481 F2).

The producer builds a v3 gate's runtime fields from ONE authoritative observation — the lane
lifecycle record + the repo's provider binding + the exact declared ProcessGenerationPin — never
hand-assembled. These tests pin: runtime_role/provider_id/assigned_name come from the declared pin
(NOT the workflow role); generation/revision/workspace from the same record; and every drift
(unbound role, missing/duplicate slot pin) fails closed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    ProcessGenerationPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_producer import (  # noqa: E402
    GateProducerError,
    build_v3_required_gate_from_observation,
    build_v3_target_from_observation,
    reissue_supersedes_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    STATE_REQUIRED,
    GateClassification,
    OriginalRequest,
)


class _Binding:
    """A minimal RoleProviderBinding-shaped stub: provider_for(role) -> provider | None."""

    def __init__(self, mapping):
        self._m = mapping

    def provider_for(self, role):
        return self._m.get(role)


def _record(*, pins=None, workspace="ws-alpha", lane="lane-alpha", generation=3, revision=1):
    return SimpleNamespace(
        repo_workspace_id=workspace,
        lane_id=lane,
        lane_generation=generation,
        revision=revision,
        declared_pins=tuple(
            pins
            if pins is not None
            else (
                ProcessGenerationPin(
                    role="claude", provider="claude", assigned_name="worker-a", locator="w1:p1"
                ),
            )
        ),
    )


def _binding():
    return _Binding({"implementation_worker": "claude", "coordinator": "codex"})


def _classification():
    return GateClassification(
        blocker_id="first_run_theme", profile_version="2", classifier_version="1", observed_at="x"
    )


def _original():
    return OriginalRequest(source="redmine", issue="13760", journal="77948", delivery_id="deliv-1")


class BuildV3TargetTests(unittest.TestCase):
    def test_populates_runtime_role_from_pin_not_workflow_role(self) -> None:
        t = build_v3_target_from_observation(
            record=_record(),
            binding=_binding(),
            workflow_role="implementation_worker",
            execution_root=".",
        )
        # runtime_role is the pin's provider-role, distinct from the workflow target_role.
        self.assertEqual(t.target_role, "implementation_worker")
        self.assertEqual(t.runtime_role, "claude")
        self.assertEqual(t.provider_id, "claude")
        self.assertEqual(t.target_assigned_name, "worker-a")
        self.assertEqual(t.workspace_id, "ws-alpha")
        self.assertEqual(t.lane_id, "lane-alpha")
        self.assertEqual(t.agent_generation, 3)
        self.assertEqual(t.lane_revision, 1)

    def test_unbound_role_fails_closed(self) -> None:
        with self.assertRaises(GateProducerError):
            build_v3_target_from_observation(
                record=_record(),
                binding=_Binding({}),  # implementation_worker unbound
                workflow_role="implementation_worker",
                execution_root=".",
            )

    def test_no_pin_for_provider_slot_fails_closed(self) -> None:
        # The binding resolves to codex, but the record only declares a claude pin.
        with self.assertRaises(GateProducerError):
            build_v3_target_from_observation(
                record=_record(),
                binding=_binding(),
                workflow_role="coordinator",  # -> codex, no codex pin declared
                execution_root=".",
            )

    def test_duplicate_pins_for_slot_fail_closed(self) -> None:
        pins = (
            ProcessGenerationPin(role="claude", provider="claude", assigned_name="a", locator="w1:p1"),
            ProcessGenerationPin(role="claude", provider="claude", assigned_name="b", locator="w1:p2"),
        )
        with self.assertRaises(GateProducerError):
            build_v3_target_from_observation(
                record=_record(pins=pins),
                binding=_binding(),
                workflow_role="implementation_worker",
                execution_root=".",
            )


class BuildV3RequiredGateTests(unittest.TestCase):
    def test_builds_required_gate_from_single_observation(self) -> None:
        gate = build_v3_required_gate_from_observation(
            record=_record(),
            binding=_binding(),
            workflow_role="implementation_worker",
            execution_root=".",
            gate_id="gate-1",
            action_generation=1,
            original_request=_original(),
            classification=_classification(),
        )
        self.assertEqual(gate.state, STATE_REQUIRED)
        self.assertEqual(gate.schema_version, 3)
        self.assertEqual(gate.target.runtime_role, "claude")
        self.assertEqual(gate.target.agent_generation, 3)
        self.assertEqual(gate.target.lane_revision, 1)
        self.assertIsNone(gate.approval)


class ReissueSupersedesNoteTests(unittest.TestCase):
    def test_names_superseded_journal(self) -> None:
        note = reissue_supersedes_note(superseded_journal="79000")
        self.assertIn("journal=79000", note)
        self.assertIn("no legacy backfill", note)

    def test_blank_journal_fails_closed(self) -> None:
        with self.assertRaises(GateProducerError):
            reissue_supersedes_note(superseded_journal="")


if __name__ == "__main__":
    unittest.main()
