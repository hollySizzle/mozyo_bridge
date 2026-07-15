"""Classical tests for the action-time operator startup-gate projection (#13812).

Hermetic tests for the read-only / dry-run projection
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection`).
They pin the negative matrix j#78409 requires — unknown / unreadable / mismatch /
newer / stale generation / ambiguous / already-clear / superseded all resolve to a
distinct fail-closed disposition — and the positive case: a real startup blocker
projects a ``required`` gate pinned to the live target and the matched blocker id.

Two guarantees get their own tests: (1) the projection is **zero-read** when it
short-circuits on identity / generation before the pane read (a
``read_visible`` that raises if called proves it never ran); (2) the classifier
reused is #13760's — the blocked / admitted cases drive the real ``claude`` profile.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_gate_projection import (  # noqa: E402
    PROJECT_AMBIGUOUS_TARGET,
    PROJECT_IDENTITY_MISMATCH,
    PROJECT_IDENTITY_UNRESOLVED,
    PROJECT_NEWER_GENERATION,
    PROJECT_OPERATOR_ACTION_REQUIRED,
    PROJECT_STALE_GENERATION,
    PROJECT_STARTUP_CLEAR,
    PROJECT_SUPERSEDED,
    PROJECT_UNKNOWN_PROVIDER,
    PROJECT_UNREADABLE,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    ObservedStartupTarget,
    OperatorStartupProjectionError,
    project_operator_startup_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (  # noqa: E402
    STATE_CONSUMED,
    STATE_SUPERSEDED,
    GateApproval,
    GateClassification,
    GateTarget,
    OperatorStartupGate,
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


def _existing_required_gate(**target_overrides) -> OperatorStartupGate:
    return build_required_gate(
        gate_id="gate-existing",
        action_generation=1,
        original_request=_original(),
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

    def test_telemetry_dict_is_token_only(self) -> None:
        result = _project(_resolved(_target()), lambda: _THEME_SCREEN)
        telemetry = result.to_telemetry_dict()
        self.assertEqual(telemetry["disposition"], PROJECT_OPERATOR_ACTION_REQUIRED)
        self.assertIn("gate", telemetry)
        # No pane text leaked into telemetry.
        blob = str(telemetry)
        self.assertNotIn("Dark mode", blob)
        self.assertNotIn("get started", blob)


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


class StaleGateFailClosedTests(unittest.TestCase):
    def test_terminal_gate_is_superseded_zero_read(self) -> None:
        terminal = build_required_gate(
            gate_id="gate-existing",
            action_generation=1,
            original_request=_original(),
            target=_target(),
            classification=GateClassification(
                blocker_id="first_run_theme",
                profile_version="2",
                classifier_version="1",
                observed_at="2026-07-15T00:00:00Z",
            ),
        )
        # Promote to a terminal state via a full record (consumed carries approval).
        consumed = OperatorStartupGate(
            gate_id=terminal.gate_id,
            action_generation=terminal.action_generation,
            state=STATE_CONSUMED,
            original_request=terminal.original_request,
            target=terminal.target,
            classification=terminal.classification,
            approval=GateApproval(source_journal="78412"),
        )
        result = _project(
            _resolved(_target()), _exploding_read(), existing_gate=consumed
        )
        self.assertEqual(result.disposition, PROJECT_SUPERSEDED)

    def test_superseded_gate_is_superseded_zero_read(self) -> None:
        superseded = OperatorStartupGate(
            gate_id="gate-existing",
            action_generation=1,
            state=STATE_SUPERSEDED,
            original_request=_original(),
            target=_target(),
            classification=GateClassification(
                blocker_id="first_run_theme",
                profile_version="2",
                classifier_version="1",
                observed_at="2026-07-15T00:00:00Z",
            ),
        )
        result = _project(
            _resolved(_target()), _exploding_read(), existing_gate=superseded
        )
        self.assertEqual(result.disposition, PROJECT_SUPERSEDED)

    def test_identity_mismatch_zero_read(self) -> None:
        existing = _existing_required_gate(lane_id="lane-beta")
        result = _project(
            _resolved(_target(lane_id="lane-alpha")),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_IDENTITY_MISMATCH)

    def test_provider_change_is_identity_mismatch(self) -> None:
        existing = _existing_required_gate(provider_id="claude")
        result = _project(
            _resolved(_target(provider_id="codex")),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_IDENTITY_MISMATCH)

    def test_newer_generation_zero_read(self) -> None:
        existing = _existing_required_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=5)),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_NEWER_GENERATION)

    def test_stale_generation_zero_read(self) -> None:
        existing = _existing_required_gate(agent_generation=5)
        result = _project(
            _resolved(_target(agent_generation=3)),
            _exploding_read(),
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_STALE_GENERATION)

    def test_matching_generation_proceeds_to_classifier(self) -> None:
        existing = _existing_required_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=3)),
            lambda: _THEME_SCREEN,
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_OPERATOR_ACTION_REQUIRED)

    def test_matching_identity_clear_pane_is_startup_clear(self) -> None:
        existing = _existing_required_gate(agent_generation=3)
        result = _project(
            _resolved(_target(agent_generation=3)),
            lambda: _READY_COMPOSER,
            existing_gate=existing,
        )
        self.assertEqual(result.disposition, PROJECT_STARTUP_CLEAR)


if __name__ == "__main__":
    unittest.main()
