"""R2 correction acceptance (Redmine #13569 j#77190 / j#77200 — F1..F5b).

The R2 review required the snapshot/binding to reach the RUNTIME handlers / discovery /
read-back (not only parser choices or leaf hand-offs), and to key on the TARGET repo's
binding. These tests drive those runtime paths and pin the target-repo authority.

Providers are fake, explicit placeholders (strengthened-scanner rule).
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

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
            {"version": "t", "source": "test-fixture", "profiles": profiles}
        ).to_registry()
    )


SYNTH = _snapshot(
    {
        "mistral-cli": {
            "protocol": "interactive_cli_tui",
            "executable": {"command": "mistral", "env_override": "MOZYO_AGENT_MISTRAL_BINARY"},
            "discovery_aliases": ["mistral-cli"],
            "process_names": ["mistral"],
            "capabilities": ["interactive_tui"],
        }
    }
)


class F1RuntimeDiscoveryUsesInjectedSnapshot(unittest.TestCase):
    def test_discover_agents_classifies_synthetic_pane_via_snapshot(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E501
            discover_agents,
        )

        pane = {"location": "s:0.0", "window_name": "mistral-cli", "command": "mistral", "cwd": "/x"}
        self.assertEqual(discover_agents([pane], snapshot=SYNTH)[0].agent_kind, "mistral-cli")
        # default (no snapshot) => unknown, proving the runtime classify keys on the injection
        self.assertEqual(discover_agents([pane])[0].agent_kind, "unknown")

    def test_no_module_level_e140_import_in_discovery_modules(self) -> None:
        import ast

        for rel in (
            "e_110_execution_platform/f_120_agent_discovery_pane_resolution/domain/agent_discovery.py",
            "e_110_execution_platform/f_120_agent_discovery_pane_resolution/domain/pane_resolver.py",
        ):
            tree = ast.parse((ROOT / "src" / "mozyo_bridge" / rel).read_text())
            module_e140 = [
                n
                for n in tree.body
                if isinstance(n, ast.ImportFrom) and n.module and "e_140" in n.module
            ]
            self.assertEqual(module_e140, [], f"{rel} still has a module-level e_140 import")

    def test_agents_targets_use_case_accepts_synthetic_via_injected_snapshot(self) -> None:
        from mozyo_bridge.application.commands_agents import ResolveAgentTargetsUseCase

        class _FakeDiscovery:
            def discover(self):
                return []

            def canonical_session(self, r):
                return None

            def checkout_facts(self, r):
                return {}

            def project_scope(self, cwd, r):
                return None

        # A synthetic `--agent` filter is accepted when the injected snapshot carries it…
        ResolveAgentTargetsUseCase(_FakeDiscovery()).resolve(
            agent_filter="mistral-cli", session_filter=None, snapshot=SYNTH
        )
        # …and rejected under the built-in default (proving the validation keys on the snapshot).
        with self.assertRaises(SystemExit):
            ResolveAgentTargetsUseCase(_FakeDiscovery()).resolve(
                agent_filter="mistral-cli", session_filter=None
            )


class F2HerdrReadBackRecognizesReboundPair(unittest.TestCase):
    def test_lane_slots_and_read_lane_use_the_binding_pair(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
        )

        ops = HerdrSublaneActuatorOps(repo_root="/repo", lane_label="lane1", issue="1")
        rows = [
            {AGENT_KEY_NAME: "mzb1_projws_grokgw_lane1", "pane_id": "wZ:p2"},
            {AGENT_KEY_NAME: "mzb1_projws_mistralcli_lane1", "pane_id": "wZ:p3"},
        ]
        # The rebound pair is recognized when the managed pair is the binding's providers…
        self.assertEqual(
            ops._lane_slots("projws", "lane1", rows, ("grokgw", "mistralcli")),
            {"grokgw": "wZ:p2", "mistralcli": "wZ:p3"},
        )
        # …and invisible against the fixed built-in pair (the R2-F2 launched-but-invisible bug).
        self.assertEqual(ops._lane_slots("projws", "lane1", rows), {})


class F3bGatewayGateUsesTargetRepoBinding(unittest.TestCase):
    def test_gate_resolves_the_binding_from_the_target_repo_root(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            gateway_route_gate as grg,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501
            RoleProviderBinding,
        )

        seen = {}

        def _fake_load(repo_root=None):
            seen["repo_root"] = repo_root
            return (RoleProviderBinding.default(), [])

        orig = grg.load_workflow_binding
        grg.load_workflow_binding = _fake_load
        try:
            target = argparse.Namespace(
                workspace_id="wsB", lane_id="laneB", role="codex", repo_root="/target-repo"
            )
            args = argparse.Namespace(allow_direct_worker=False)
            emitted = []
            grg.enforce_gateway_route(
                args,
                kind="reply",  # non-governed => allowed, but the binding load still runs
                receiver="codex",
                preflight_target=target,
                source="redmine",
                mode="standard",
                anchor=None,
                target="%1",
                record_format="both",
                record_command=None,
                emit=lambda *a, **k: emitted.append(a),
                sender_lane_unit=("wsA", "laneA"),
            )
        finally:
            grg.load_workflow_binding = orig
        # The gate loaded the binding from the TARGET repo, not the sender's cwd (R2-F3b).
        self.assertEqual(seen["repo_root"], "/target-repo")


class F4bRetireLaunchabilityAndPerUnit(unittest.TestCase):
    def _rows(self, *specs):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
        )

        return [
            {AGENT_KEY_NAME: f"mzb1_{ws}_{role}_{lane}", "pane_id": pid}
            for ws, role, lane, pid in specs
        ]

    def test_cross_unit_substitution_is_not_masked(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            plan_herdr_retire_close,
        )

        # shared codex+claude (substitution) + legacy twin mistralcli, expected codex/mistralcli
        rows = self._rows(
            ("projws", "codex", "lane1", "a"),
            ("projws", "claude", "lane1", "b"),
            ("wt_deadbeef", "mistralcli", "default", "c"),
        )
        plan = plan_herdr_retire_close(
            rows,
            workspace_id="projws",
            lane_id="lane1",
            legacy_workspace_id="wt_deadbeef",
            managed_roles=("codex", "mistralcli"),
        )
        self.assertEqual(plan.close_targets, ())

    def test_unknown_managed_provider_launchability_gate(self) -> None:
        # The built-in snapshot rejects an unknown managed provider as unlaunchable — the
        # retire command boundary uses exactly this predicate to zero-actuate (R2-F4b).
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_runtime import (  # noqa: E501
            BUILTIN_AGENT_PROVIDER_SNAPSHOT,
        )

        self.assertFalse(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable("nope-cli"))
        self.assertTrue(BUILTIN_AGENT_PROVIDER_SNAPSHOT.is_launchable("codex"))


class F5bSelectorUsesTargetRepoWorkerBinding(unittest.TestCase):
    def test_worker_provider_resolved_from_expected_target_repo(self) -> None:
        from mozyo_bridge.application import commands_target_select as cts

        seen = {}

        def _fake_resolve(repo_root=None):
            seen["repo_root"] = repo_root
            return "claude"

        # Patch the resolver imported inside select_semantic_target's function body.
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution as wpr

        orig = wpr.resolve_worker_provider
        wpr.resolve_worker_provider = _fake_resolve
        try:
            cts.select_semantic_target(
                role="codex",
                repo="/target-repo",
                session=None,
                project=None,
                sender_cwd="/sender-repo",
                candidates=[],
            )
        except SystemExit:
            # 0 candidates fails closed after resolution — we only assert the repo used.
            pass
        finally:
            wpr.resolve_worker_provider = orig
        # The worker provider was resolved from the TARGET repo (`--target-repo`), not sender.
        self.assertEqual(seen["repo_root"], str(Path("/target-repo").expanduser().resolve()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
