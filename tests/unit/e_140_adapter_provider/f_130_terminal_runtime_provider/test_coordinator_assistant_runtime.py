"""Role-aware coordinator-unit runtime acceptance tests (Redmine #15687)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agents_topology import (  # noqa: E501
    AgentsTopologyError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    MOZYO_WORKFLOW_ROLE_ENV,
    build_agent_start_argv,
    build_pane_launch_env,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_service import (  # noqa: E501
    prepare_configured_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    _prepare_session_locked,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    ResolvedProviderLaunch,
)


def _config() -> RepoLocalConfig:
    return RepoLocalConfig.from_record(
        {
            "version": 2,
            "agents": {
                "profiles": {
                    "coordinator_primary": {
                        "provider": "claude",
                        "launch_argv": {
                            "default": ["--model", "claude-fable-5"]
                        },
                    },
                    "coordinator_assistance": {
                        "provider": "codex",
                        "launch_argv": {
                            "default": [
                                "--model",
                                "gpt-5.6-sol",
                                "--config",
                                "model_reasoning_effort=xhigh",
                            ]
                        },
                    },
                },
                "roles": {
                    "coordinator": "coordinator_primary",
                    "coordinator_assistant": "coordinator_assistance",
                },
            },
        }
    )


class ConfiguredCoordinatorUnitTest(unittest.TestCase):
    def test_committed_binding_is_exact_and_sublane_profiles_are_unchanged(self) -> None:
        root = next(
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / "pyproject.toml").is_file()
        )
        topology = load_repo_local_config(root).agents
        slots = {slot.workflow_role: slot for slot in topology.resolve_coordinator_unit()}

        self.assertEqual(slots["coordinator"].provider, "claude")
        self.assertEqual(slots["coordinator"].launch_argv, ("--model", "claude-fable-5"))
        self.assertEqual(slots["coordinator_assistant"].provider, "codex")
        self.assertEqual(
            slots["coordinator_assistant"].launch_argv,
            (
                "--model",
                "gpt-5.6-sol",
                "--config",
                "model_reasoning_effort=xhigh",
            ),
        )
        self.assertEqual(
            topology.resolve_launch_argv_for_role(
                "implementation_worker", "sublane"
            ),
            ["--model", "claude-fable-5"],
        )
        self.assertEqual(
            topology.resolve_launch_argv_for_role(
                "project_gateway", "sublane"
            ),
            ["--config", "model_reasoning_effort=high"],
        )

    def test_one_provider_cannot_impersonate_both_coordinator_unit_roles(self) -> None:
        config = RepoLocalConfig.from_record(
            {
                "version": 2,
                "agents": {
                    "profiles": {
                        "primary": {"provider": "claude"},
                        "assistant": {"provider": "claude"},
                    },
                    "roles": {
                        "coordinator": "primary",
                        "coordinator_assistant": "assistant",
                    },
                },
            }
        )
        with self.assertRaisesRegex(
            AgentsTopologyError,
            "cannot attest as separate actors",
        ):
            config.agents.resolve_coordinator_unit()

    def test_service_projects_role_specific_models_without_rebinding_sublanes(self) -> None:
        captured = {}

        def prepare(**call):
            captured.update(call)
            return call

        service_module = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_start_service"
        )
        with (
            patch(f"{service_module}.load_repo_local_config", return_value=_config()),
            patch(
                f"{service_module}.load_coordinator_placement_for_launch",
                return_value=SimpleNamespace(
                    mode="per_project_space", top_workspace_id=""
                ),
            ),
        ):
            prepare_configured_session(
                repo_root=Path("/repo"),
                agents=["claude", "codex"],
                lane_id="",
                env={},
                dry_run=True,
                claude_permission_mode_default="auto",
                session_preparer=prepare,
            )

        self.assertEqual(
            captured["workflow_role_by_provider"],
            {"claude": "coordinator", "codex": "coordinator_assistant"},
        )
        self.assertEqual(
            list(captured["launch_argv_by_provider"]["claude"]),
            ["--model", "claude-fable-5"],
        )
        self.assertEqual(
            list(captured["launch_argv_by_provider"]["codex"]),
            [
                "--model",
                "gpt-5.6-sol",
                "--config",
                "model_reasoning_effort=xhigh",
            ],
        )

    def test_v1_provider_keyed_default_launch_argv_is_not_discarded(self) -> None:
        captured = {}

        def prepare(**call):
            captured.update(call)
            return call

        legacy = RepoLocalConfig.from_record(
            {
                "version": 1,
                "agent_launch": {
                    "launch_argv": {
                        "claude": {"default": ["--model", "legacy-claude-model"]},
                        "codex": {
                            "default": [
                                "--model",
                                "legacy-codex-model",
                                "--config",
                                "model_reasoning_effort=high",
                            ]
                        },
                    }
                },
            }
        )
        service_module = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_start_service"
        )
        with (
            patch(f"{service_module}.load_repo_local_config", return_value=legacy),
            patch(
                f"{service_module}.load_coordinator_placement_for_launch",
                return_value=SimpleNamespace(
                    mode="per_project_space", top_workspace_id=""
                ),
            ),
        ):
            prepare_configured_session(
                repo_root=Path("/repo"),
                agents=["claude", "codex"],
                lane_id="",
                env={},
                dry_run=True,
                claude_permission_mode_default="auto",
                session_preparer=prepare,
            )

        self.assertEqual(
            captured["workflow_role_by_provider"],
            {"claude": "coordinator_assistant", "codex": "coordinator"},
        )
        self.assertEqual(
            list(captured["launch_argv_by_provider"]["claude"]),
            ["--model", "legacy-claude-model"],
        )
        self.assertEqual(
            list(captured["launch_argv_by_provider"]["codex"]),
            [
                "--model",
                "legacy-codex-model",
                "--config",
                "model_reasoning_effort=high",
            ],
        )

    def test_launch_argv_and_env_keep_provider_and_workflow_role_separate(self) -> None:
        resolved = ResolvedProviderLaunch(
            provider_id="codex",
            executable="/opt/codex",
            argv0="/opt/codex",
        )
        argv = build_agent_start_argv(
            assigned_name="mzb1_ws1_codex_default",
            native_name="mza1_aaaaaaaaaaaaaaaaaaaaaaaaaaa",
            pane_locator="w1:p1",
            provider="codex",
            workflow_role="coordinator_assistant",
            workspace_id="ws1",
            lane="default",
            attest_launcher="/opt/mozyo-bridge",
            resolved=resolved,
            launch_argv_extra=(
                "--model",
                "gpt-5.6-sol",
                "--config",
                "model_reasoning_effort=xhigh",
            ),
        )
        self.assertEqual(argv[argv.index("--kind") + 1], "codex")
        self.assertIn("--workflow-role", argv)
        self.assertEqual(
            argv[argv.index("--workflow-role") + 1], "coordinator_assistant"
        )
        self.assertIn("gpt-5.6-sol", argv)
        self.assertIn("model_reasoning_effort=xhigh", argv)

        pane_env = build_pane_launch_env(
            provider="codex",
            workflow_role="coordinator_assistant",
            native_name="mza1_aaaaaaaaaaaaaaaaaaaaaaaaaaa",
            workspace_id="ws1",
            lane="default",
            binary="/opt/herdr",
            source_path="/usr/bin",
            attest_launcher="/opt/mozyo-bridge",
            store_home="/store",
            resolved=resolved,
        )
        self.assertIn("MOZYO_AGENT_ROLE=codex", pane_env)
        self.assertIn(
            f"{MOZYO_WORKFLOW_ROLE_ENV}=coordinator_assistant", pane_env
        )

    def test_private_restore_entry_revalidates_role_projection_before_effects(self) -> None:
        with self.assertRaisesRegex(
            HerdrSessionStartError,
            "workflow role 'coordinator' is assigned twice",
        ):
            _prepare_session_locked(
                repo_root=Path("/repo"),
                providers=["claude", "codex"],
                lane_id="",
                env={},
                dry_run=True,
                workflow_role_by_provider={
                    "claude": "coordinator",
                    "codex": "coordinator",
                },
                launch_argv_by_provider={"claude": (), "codex": ()},
            )


if __name__ == "__main__":
    unittest.main()
