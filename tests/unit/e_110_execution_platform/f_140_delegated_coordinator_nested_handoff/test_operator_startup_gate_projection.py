"""Classical tests for the action-time operator startup-gate projection (#13812).

Hermetic tests for the read-only / dry-run projection
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection`).
They pin the negative matrix — unknown / unreadable / mismatch / newer / stale
generation / ambiguous / already-clear / gate-binding-mismatch all resolve to a
distinct fail-closed disposition — and the positive case: a real startup blocker
projects a ``required`` gate pinned to the live target and the matched blocker id.

Guarantees with their own tests: (1) the projection is **zero-read** when it
short-circuits on identity / binding / generation before the pane read (a
``read_visible`` that raises if called proves it never ran); (2) the reused
classifier is #13760's; (3) the gate-binding fence (review j#79003 Finding 1)
refuses a re-projection that re-binds an existing gate to a new action generation;
(4) ``detail`` never enters ``to_telemetry_dict`` (review j#79003 Finding 4).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (  # noqa: E402
    PROJECT_AMBIGUOUS_TARGET,
    PROJECT_GATE_BINDING_MISMATCH,
    PROJECT_IDENTITY_MISMATCH,
    PROJECT_IDENTITY_UNRESOLVED,
    PROJECT_NEWER_GENERATION,
    PROJECT_OPERATOR_ACTION_REQUIRED,
    PROJECT_STALE_GENERATION,
    PROJECT_STARTUP_CLEAR,
    PROJECT_UNKNOWN_PROVIDER,
    PROJECT_UNREADABLE,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    ObservedStartupTarget,
    OperatorStartupGateProjection,
    OperatorStartupProjectionError,
    project_operator_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    GateClassification,
    GateTarget,
    OriginalRequest,
    build_required_gate,
    repo_identity_digest,
)

# Signatures from the real `claude` profile (agent_provider_profiles.yaml).
_THEME_SCREEN = (
    "Let's get started\n"
    "Choose the text style that looks best with your terminal\n"
    "> Dark mode"
)
_READY_COMPOSER = "esc to interrupt\n> \nType your message and press enter"


def _target(**overrides) -> GateTarget:
    kwargs = dict(
        workspace_id="ws-alpha",
        repo_identity_digest=repo_identity_digest("repo-alpha"),
        execution_root=".",
        lane_id="lane-alpha",
        target_role="implementation_worker",
        target_assigned_name="worker-a",
        provider_id="claude",
        agent_generation=3,
    )
    kwargs.update(overrides)
    return GateTarget(**kwargs)


def _original() -> OriginalRequest:
    return OriginalRequest(
        source="redmine", issue="13760", journal="77948", delivery_id="deliv-1"
    )


def _resolved(target: GateTarget) -> ObservedStartupTarget:
    return ObservedStartupTarget(resolution=RESOLUTION_RESOLVED, target=target)


def _project(observed, read_visible, *, existing_gate=None):
    return project_operator_startup_gate(
        observed=observed,
        read_visible=read_visible,
        original_request=_original(),
        gate_id="gate-1",
        action_generation=1,
        profile_version="2",
        classifier_version="1",
        observed_at="2026-07-16T00:00:00Z",
        existing_gate=existing_gate,
    )


def _exploding_read():
    def _read():
        raise AssertionError("read_visible must not be called on a short-circuit")

    return _read


def _existing_gate(*, gate_id="gate-1", action_generation=1, original_request=None, **target_overrides):
    # Defaults match `_project`'s caller args so the gate-binding fence passes and the
    # stale判定 (identity / generation) is what a test exercises. A test that wants a
    # binding mismatch overrides gate_id / action_generation / original_request.
    return build_required_gate(
        gate_id=gate_id,
        action_generation=action_generation,
        original_request=original_request or _original(),
        target=_target(**target_overrides),
        classification=GateClassification(
            blocker_id="first_run_theme",
            profile_version="2",
            classifier_version="1",
            observed_at="2026-07-15T00:00:00Z",
        ),
    )


class ObservedStartupTargetTests(unittest.TestCase):
    def test_resolved_requires_target(self) -> None:
        with self.assertRaises(OperatorStartupProjectionError):
            ObservedStartupTarget(resolution=RESOLUTION_RESOLVED, target=None)

    def test_ambiguous_must_not_carry_target(self) -> None:
        with self.assertRaises(OperatorStartupProjectionError):
            ObservedStartupTarget(resolution=RESOLUTION_AMBIGUOUS, target=_target())

    def test_unknown_resolution_rejected(self) -> None:
        with self.assertRaises(OperatorStartupProjectionError):
            ObservedStartupTarget(resolution="maybe")


class PositiveProjectionTests(unittest.TestCase):
    def test_blocker_projects_required_gate(self) -> None:
        result = _project(_resolved(_target()), lambda: _THEME_SCREEN)
        self.assertEqual(result.disposition, PROJECT_OPERATOR_ACTION_REQUIRED)
        self.assertTrue(result.requires_operator_action)
        self.assertIsNotNone(result.gate)
        assert result.gate is not None
        self.assertEqual(result.gate.classification.blocker_id, "first_run_theme")
        self.assertEqual(result.gate.target, _target())
        self.assertEqual(result.gate.action_generation, 1)
        self.assertIsNone(result.gate.approval)

    def test_ready_composer_is_startup_clear(self) -> None:
        result = _project(_resolved(_target()), lambda: _READY_COMPOSER)
        self.assertEqual(result.disposition, PROJECT_STARTUP_CLEAR)
        self.assertFalse(result.requires_operator_action)
        self.assertIsNone(result.gate)


class TelemetryTests(unittest.TestCase):
    def test_telemetry_excludes_detail_and_pane_text(self) -> None:
        result = _project(_resolved(_target()), lambda: _THEME_SCREEN)
        telemetry = result.to_telemetry_dict()
        self.assertEqual(telemetry["disposition"], PROJECT_OPERATOR_ACTION_REQUIRED)
        self.assertIn("gate", telemetry)
        # Finding 4: free-form detail is never part of the pasteable telemetry.
        self.assertNotIn("detail", telemetry)
        blob = str(telemetry)
        self.assertNotIn("Dark mode", blob)
        self.assertNotIn("get started", blob)

    def test_detail_with_path_never_reaches_telemetry(self) -> None:
        # Even a (hypothetical) leaky detail cannot enter the machine-readable surface.
        leaky = OperatorStartupGateProjection(
            disposition=PROJECT_UNREADABLE,
            detail="failed reading /Users/secret/path token=abc",
        )
        telemetry = leaky.to_telemetry_dict()
        self.assertNotIn("detail", telemetry)
        self.assertNotIn("/Users/secret", str(telemetry))


class StartupClassifierFailClosedTests(unittest.TestCase):
    def test_unreadable_pane(self) -> None:
        def _raises():
            raise RuntimeError("transport down")

        result = _project(_resolved(_target()), _raises)
        self.assertEqual(result.disposition, PROJECT_UNREADABLE)
        self.assertIsNone(result.gate)

    def test_blank_pane_is_unreadable_not_clear(self) -> None:
        result = _project(_resolved(_target()), lambda: "   ")
        self.assertEqual(result.disposition, PROJECT_UNREADABLE)

    def test_unknown_provider(self) -> None:
        result = _project(_resolved(_target(provider_id="ghostprovider")), lambda: "x")
        self.assertEqual(result.disposition, PROJECT_UNKNOWN_PROVIDER)


class IdentityResolutionFailClosedTests(unittest.TestCase):
    def test_ambiguous_target_zero_read(self) -> None:
        result = _project(
            ObservedStartupTarget(resolution=RESOLUTION_AMBIGUOUS), _exploding_read()
        )
        self.assertEqual(result.disposition, PROJECT_AMBIGUOUS_TARGET)

    def test_unresolved_target_zero_read(self) -> None:
        result = _project(
            ObservedStartupTarget(resolution=RESOLUTION_UNRESOLVED), _exploding_read()
        )
        self.assertEqual(result.disposition, PROJECT_IDENTITY_UNRESOLVED)


class GateBindingFenceTests(unittest.TestCase):
    # Finding 1: a re-projection must continue the SAME gate under the SAME action
    # generation; a divergence fails closed BEFORE any read.
    def test_newer_action_generation_is_binding_mismatch_zero_read(self) -> None:
        existing = _existing_gate(action_generation=2)  # caller passes 1
        result = _project(_resolved(_target()), _exploding_read(), existing_gate=existing)
        self.assertEqual(result.disposition, PROJECT_GATE_BINDING_MISMATCH)

    def test_older_action_generation_is_binding_mismatch_zero_read(self) -> None:
        # Caller `_project` passes action_generation=1; the existing gate is at 5, so
        # the caller's action generation is OLDER than the gate's — still a mismatch.
        existing = _existing_gate(action_generation=5)
        result = _project(_resolved(_target()), _exploding_read(), existing_gate=existing)
        self.assertEqual(result.disposition, PROJECT_GATE_BINDING_MISMATCH)

    def test_different_gate_id_is_binding_mismatch_zero_read(self) -> None:
        existing = _existing_gate(gate_id="gate-other")
        result = _project(_resolved(_target()), _exploding_read(), existing_gate=existing)
        self.assertEqual(result.disposition, PROJECT_GATE_BINDING_MISMATCH)

    def test_different_original_request_is_binding_mismatch_zero_read(self) -> None:
        other_original = OriginalRequest(
            source="redmine", issue="99999", journal="88888", delivery_id="deliv-z"
        )
        existing = _existing_gate(original_request=other_original)
        result = _project(_resolved(_target()), _exploding_read(), existing_gate=existing)
        self.assertEqual(result.disposition, PROJECT_GATE_BINDING_MISMATCH)


class StaleGenerationFailClosedTests(unittest.TestCase):
    # Binding matches (gate_id / action_generation / original_request equal); the
    # stale判定 is on the TARGET identity / agent generation.
    def test_identity_mismatch_zero_read(self) -> None:
        existing = _existing_gate(lane_id="lane-beta")
        result = _project(
            _resolved(_target(lane_id="lane-alpha")),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_IDENTITY_MISMATCH)

    def test_provider_change_is_identity_mismatch(self) -> None:
        existing = _existing_gate(provider_id="claude")
        result = _project(
            _resolved(_target(provider_id="codex")),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_IDENTITY_MISMATCH)

    def test_newer_agent_generation_zero_read(self) -> None:
        existing = _existing_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=5)),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_NEWER_GENERATION)

    def test_stale_agent_generation_zero_read(self) -> None:
        existing = _existing_gate(agent_generation=5)
        result = _project(
            _resolved(_target(agent_generation=3)),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_STALE_GENERATION)

    def test_matching_gate_still_blocked_projects_required(self) -> None:
        existing = _existing_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=3)),
            lambda: _THEME_SCREEN,
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_OPERATOR_ACTION_REQUIRED)

    def test_matching_gate_clear_pane_is_startup_clear(self) -> None:
        existing = _existing_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=3)),
            lambda: _READY_COMPOSER,
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_STARTUP_CLEAR)


if __name__ == "__main__":
    unittest.main()
