"""Project-gateway send-effect wiring tests (Redmine #15118)."""

from __future__ import annotations

import argparse
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mozyo_bridge.application import handoff_transport_wiring as wiring
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry import (
    PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
    RESOLVED_TARGET_CAPABILITY_ARG,
    HerdrSendEntryError,
    ResolvedHerdrTargetCapability,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    TransportBinding,
    TransportBindingError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    PaneReadResult,
    TerminalTransportConfig,
    TransportResult,
)


class _RecordingTransport:
    backend = BACKEND_HERDR

    def __init__(self) -> None:
        self.effects: list[tuple[str, str]] = []

    def send_text(self, target: str, text: str) -> TransportResult:
        self.effects.append(("text", target))
        return TransportResult.success()

    def send_keys(self, target: str, keys: str) -> TransportResult:
        self.effects.append(("keys", target))
        return TransportResult.success()

    def read_pane(self, target: str, **_kwargs) -> PaneReadResult:
        return PaneReadResult.success("ready")


def _binding(calls: list[tuple[str, ...]]) -> TransportBinding:
    def run_tmux(*args: str, check: bool = True):
        del check
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return TransportBinding(
        backend=BACKEND_HERDR,
        run_tmux=run_tmux,
        capture_pane=lambda _target, _lines: "ready",
    )


class ProjectGatewayEffectGuardTest(unittest.TestCase):
    def test_nonstandard_binding_refuses_generation_drift_before_any_effect(self):
        calls: list[tuple[str, ...]] = []
        verifier = Mock(
            side_effect=HerdrSendEntryError(
                "generation changed", reason="resolved_target_capability_mismatch"
            )
        )
        guarded = wiring._guard_project_gateway_binding_effects(
            _binding(calls), verifier
        )

        with self.assertRaises(TransportBindingError):
            guarded.run_tmux(
                "send-keys", "-t", "w1:p1", "-l", "--", "body"
            )

        self.assertEqual(calls, [])
        verifier.assert_called_once_with()

    def test_nonstandard_binding_rechecks_body_and_enter_but_not_capture(self):
        calls: list[tuple[str, ...]] = []
        verifier = Mock()
        guarded = wiring._guard_project_gateway_binding_effects(
            _binding(calls), verifier
        )

        self.assertEqual(guarded.capture_pane("w1:p1", 10), "ready")
        guarded.run_tmux("send-keys", "-t", "w1:p1", "-l", "--", "body")
        guarded.run_tmux("send-keys", "-t", "w1:p1", "Enter")

        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(len(calls), 2)

    def test_standard_rail_refuses_drift_before_transport_send(self):
        delegate = _RecordingTransport()
        verifier = Mock(
            side_effect=HerdrSendEntryError(
                "generation changed", reason="resolved_target_capability_mismatch"
            )
        )
        rail = SimpleNamespace(_transport=delegate)
        wiring._guard_project_gateway_standard_rail_effects(rail, verifier)

        result = rail._transport.send_text("w1:p1", "body")

        self.assertFalse(result.ok)
        self.assertEqual(delegate.effects, [])
        verifier.assert_called_once_with()

    def test_runtime_composition_connects_both_effect_boundaries(self):
        calls: list[tuple[str, ...]] = []
        binding = _binding(calls)
        delegate = _RecordingTransport()
        rail = SimpleNamespace(_transport=delegate)
        capability = ResolvedHerdrTargetCapability(
            workspace_id="workspace-1",
            lane_id="default",
            provider="codex",
            assigned_name="mzb1_target",
            locator="w1:p1",
            purpose=PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
            generation_token="generation-1",
            project_scope="project-1",
            target_repo_root="/target",
            target_cwd="/target",
            project_path=".",
        )
        args = argparse.Namespace(repo="/source", target="mzb1_target")
        setattr(args, RESOLVED_TARGET_CAPABILITY_ARG, capability)
        config = TerminalTransportConfig(backend=BACKEND_HERDR)
        verifier = Mock()

        with patch.object(
            wiring,
            "load_repo_local_config",
            return_value=SimpleNamespace(terminal_transport=config),
        ), patch.object(
            wiring, "_resolve_herdr_binding", return_value=binding
        ), patch.object(
            wiring, "resolve_turn_start_rail", return_value=rail
        ), patch.object(
            wiring, "verify_project_gateway_target_effect", verifier
        ):
            guarded_binding, guarded_rail = wiring.resolve_handoff_transport_runtime(args)

        guarded_binding.run_tmux(
            "send-keys", "-t", "w1:p1", "-l", "--", "body"
        )
        standard_result = guarded_rail._transport.send_text("w1:p1", "body")

        self.assertTrue(standard_result.ok)
        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(calls[0][0], "send-keys")
        self.assertEqual(delegate.effects, [("text", "w1:p1")])
        self.assertEqual(
            verifier.call_args.kwargs["repo_root"], Path("/source").resolve()
        )

    def test_target_herdr_capability_never_falls_through_sender_tmux(self):
        capability = ResolvedHerdrTargetCapability(
            workspace_id="workspace-1",
            lane_id="default",
            provider="codex",
            assigned_name="mzb1_target",
            locator="w1:p1",
            purpose=PROJECT_GATEWAY_TARGET_CAPABILITY_PURPOSE,
            generation_token="generation-1",
            project_scope="project-1",
            target_repo_root="/target",
            target_cwd="/target",
            project_path=".",
        )
        args = argparse.Namespace(repo="/source", target="mzb1_target")
        setattr(args, RESOLVED_TARGET_CAPABILITY_ARG, capability)
        source_tmux = TerminalTransportConfig.default()
        target_herdr = TerminalTransportConfig(backend=BACKEND_HERDR)
        resolver = Mock()

        with patch.object(
            wiring,
            "load_repo_local_config",
            side_effect=(
                SimpleNamespace(terminal_transport=source_tmux),
                SimpleNamespace(terminal_transport=target_herdr),
            ),
        ), patch.object(wiring, "_resolve_herdr_binding", resolver):
            with self.assertRaises(SystemExit):
                wiring.resolve_handoff_transport_runtime(args)

        resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
