"""R1 correction acceptance (Redmine #13569 j#77142 / j#77145 — F1..F5).

The R1 review required the acceptance to exercise the REAL composition (not hand-passed
leaf values), the third-provider gateway-bypass refusal, unknown/unlaunchable/mismatch
zero-actuation, and the atomic-retire pair attestation. These tests drive those paths.

Providers here are fake, explicit placeholders (strengthened-scanner rule).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E402
    build_runtime_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E402
    AgentProviderProfileConfig,
)


def _snapshot(profiles: dict):
    return build_runtime_snapshot(
        AgentProviderProfileConfig.from_record(
            {"version": "1", "source": "test-fixture", "profiles": profiles}
        ).to_registry()
    )


SYNTH = _snapshot(
    {
        "mistral-cli": {
            "protocol": "interactive_cli_tui",
            "executable": {
                "command": "mistral",
                "env_override": "MOZYO_AGENT_MISTRAL_BINARY",
            },
            "discovery_aliases": ["mistral-cli"],
            "process_names": ["mistral"],
            "capabilities": ["interactive_tui", "launch_argv_override"],
        }
    }
)


class F1RealCompositionThreadsOneSnapshot(unittest.TestCase):
    """The real parser composition threads ONE injected snapshot to every choice surface."""

    def setUp(self) -> None:
        import mozyo_bridge.application.cli as cli

        self.parser = cli.build_parser(snapshot=SYNTH)
        self.default_parser = cli.build_parser()

    def test_agents_choice_from_injected_snapshot(self) -> None:
        ns = self.parser.parse_args(["agents", "list", "--agent", "mistral-cli"])
        self.assertEqual(ns.agent, "mistral-cli")

    def test_handoff_to_choice_from_injected_snapshot(self) -> None:
        ns = self.parser.parse_args(
            ["handoff", "send", "--to", "mistral-cli", "--source", "redmine", "--kind", "reply"]
        )
        self.assertEqual(ns.to, "mistral-cli")

    def test_message_select_role_from_injected_snapshot(self) -> None:
        ns = self.parser.parse_args(["message", "x", "y", "--select-role", "mistral-cli"])
        self.assertEqual(ns.select_role, "mistral-cli")

    def test_init_agent_choice_from_injected_snapshot(self) -> None:
        ns = self.parser.parse_args(["init", "mistral-cli"])
        self.assertEqual(ns.agent, "mistral-cli")

    def test_default_composition_rejects_the_synthetic_provider(self) -> None:
        # No global monkeypatch: the synthetic provider is only visible through the
        # injected snapshot; the default composition (built-in snapshot) rejects it.
        with self.assertRaises(SystemExit):
            self.default_parser.parse_args(["agents", "list", "--agent", "mistral-cli"])


class F1StatusKnownProviderInjection(unittest.TestCase):
    def test_status_recognizes_a_window_from_the_injected_known_snapshot(self) -> None:
        from mozyo_bridge.application.commands_status import (
            ResolveSessionStatusUseCase,
            StatusQuery,
        )

        class _Fake:
            def session_exists(self, s):
                return True

            def list_windows(self, s):
                return ["mistral-cli", "shell"]

            def capture_panes(self, s):
                return (True, "panes")

        view = ResolveSessionStatusUseCase(_Fake()).resolve(
            StatusQuery(session="s"),
            known_providers=SYNTH,
            expected_providers=("mistral-cli",),
        )
        # The mistral-cli window is recognized via the injected snapshot (not import-time
        # AGENT_LABELS), and it is present so nothing is missing.
        self.assertEqual(view.agent_windows, ("mistral-cli",))
        self.assertEqual(view.missing_agents, ())


class F2LaunchPreflightZeroStart(unittest.TestCase):
    def test_unlaunchable_bound_provider_blocks_before_actuation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_actuator,
            workflow_provider_resolution as wpr,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
            ROLE_IMPLEMENTER,
            RoleProviderBinding,
        )

        orig = wpr.load_workflow_binding
        # A binding whose worker is an UNKNOWN provider (no profile) is not launchable.
        wpr.load_workflow_binding = lambda repo_root=None: (
            RoleProviderBinding.default().with_overrides({ROLE_IMPLEMENTER: "nope-cli"}),
            [],
        )
        try:
            self.assertTrue(
                sublane_actuator._sublane_start_provider_preflight_blocked("/repo")
            )
            # built-in binding passes (byte-invariant)
            wpr.load_workflow_binding = lambda repo_root=None: (
                RoleProviderBinding.default(),
                [],
            )
            self.assertFalse(
                sublane_actuator._sublane_start_provider_preflight_blocked("/repo")
            )
        finally:
            wpr.load_workflow_binding = orig


class F3GatewayGateExactGateway(unittest.TestCase):
    def test_third_provider_cross_boundary_governed_send_is_blocked(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (  # noqa: E501
            BLOCKED_NON_GATEWAY_RECEIVER,
            GatewayRouteRequest,
            ROUTE_BLOCKED,
            decide_gateway_route,
        )

        d = decide_gateway_route(
            GatewayRouteRequest(
                kind="implementation_request",
                receiver="grok-gw",
                sender_identity_known=True,
                sender_workspace_id="wsA",
                sender_lane_id="laneA",
                target_workspace_id="wsB",
                target_lane_id="laneB",
                worker_provider="mistral-cli",
                gateway_provider="codex",
            )
        )
        self.assertEqual(d.verdict, ROUTE_BLOCKED)
        self.assertEqual(d.blocked_reason, BLOCKED_NON_GATEWAY_RECEIVER)

    def test_bound_gateway_provider_is_the_allowed_route_head(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_route_enforcement import (  # noqa: E501
            GatewayRouteRequest,
            ROUTE_ALLOWED,
            decide_gateway_route,
        )

        d = decide_gateway_route(
            GatewayRouteRequest(
                kind="implementation_request",
                receiver="grok-gw",
                sender_identity_known=True,
                sender_workspace_id="wsA",
                sender_lane_id="laneA",
                target_workspace_id="wsB",
                target_lane_id="laneB",
                worker_provider="mistral-cli",
                gateway_provider="grok-gw",
            )
        )
        self.assertEqual(d.verdict, ROUTE_ALLOWED)


class F4RetireZeroCloseOnMismatch(unittest.TestCase):
    def test_provider_substitution_closes_nothing(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            plan_herdr_retire_close,
        )

        # live pair codex/claude, binding expects codex/mistralcli -> substitution -> 0
        rows = [
            {AGENT_KEY_NAME: "mzb1_projws_codex_lane1", "pane_id": "wZ:p2"},
            {AGENT_KEY_NAME: "mzb1_projws_claude_lane1", "pane_id": "wZ:p3"},
        ]
        plan = plan_herdr_retire_close(
            rows, workspace_id="projws", lane_id="lane1", managed_roles=("codex", "mistralcli")
        )
        self.assertEqual(plan.close_targets, ())


class F5LiveTargetSelectThreadsBinding(unittest.TestCase):
    def test_select_semantic_target_resolves_worker_provider_from_binding(self) -> None:
        # A rebound worker's cross-workspace candidate is refused because the live caller
        # threads the binding-resolved worker provider (not the literal `claude` default).
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E501
            CONFIDENCE_STRONG,
            ROLE_SOURCE_PANE_OPTION,
            TargetCandidate,
        )
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import (  # noqa: E501
            target_selector,
        )

        candidate = TargetCandidate(
            pane_id="%1",
            role="mistral-cli",
            role_source=ROLE_SOURCE_PANE_OPTION,
            confidence=CONFIDENCE_STRONG,
            ambiguous=False,
            session="s",
            window_name="cockpit",
            window_index="0",
            pane_index="0",
            active=False,
            workspace_id="w",
            workspace_label="w",
            lane_id="default",
            lane_label=None,
            repo_short="repo",
            repo_root="/other-repo",
            cwd="/other-repo",
            host="local",
            view_kind="cockpit_pane",
            branch="main",
        )
        query = target_selector.TargetSelectorQuery(
            role="mistral-cli", sender_repo_root="/sender-repo"
        )
        # With the binding worker provider = mistral-cli, a cross-workspace mistral-cli
        # candidate is refused (the gateway-via invariant, keyed on the binding provider).
        # The snapshot makes mistral-cli a selectable role (2A); worker_provider makes the
        # cross-workspace refusal key on it (F5).
        sel = target_selector.select_target(
            [candidate], query, snapshot=SYNTH, worker_provider="mistral-cli"
        )
        self.assertEqual(sel.status, target_selector.SELECT_CROSS_WORKSPACE_CLAUDE)
        # With the worker provider left at the built-in default (claude), the mistral-cli
        # candidate is NOT treated as the worker, so the refusal does not fire — proving the
        # cross-workspace guard keys on the injected worker provider, not the literal.
        sel_default = target_selector.select_target([candidate], query, snapshot=SYNTH)
        self.assertNotEqual(
            sel_default.status, target_selector.SELECT_CROSS_WORKSPACE_CLAUDE
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
