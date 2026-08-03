from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_retirement import (
    WorkspaceRetirementInventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.workspace_retirement_inventory import (
    HerdrWorkspaceRetirementInventory,
)


class _Agent:
    def __init__(self, *, name="agent", workspace_id="target", managed=True):
        self.name = name
        self.workspace_id = workspace_id
        self.managed = managed


class _View:
    def __init__(self, agents=(), *, raw=None, invalid=0, ok=True):
        self.backend_selected = True
        self.ok = ok
        self.agents = tuple(agents)
        self.raw_row_count = len(self.agents) if raw is None else raw
        self.invalid_row_count = invalid


class WorkspaceRetirementRegressionTests(unittest.TestCase):
    def test_parser_is_dry_run_by_default_and_execute_needs_explicit_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["workspace", "retire", "--workspace-id", "target", "--json"]
        )
        self.assertEqual(args.workspace_command, "retire")
        self.assertFalse(args.execute)
        self.assertEqual(args.expect_plan_digest, "")

    def test_global_inventory_counts_only_exact_target_and_detects_loss(self) -> None:
        adapter = HerdrWorkspaceRetirementInventory(
            repo_root=Path("."), env={}
        )
        module = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.workspace_retirement_inventory.read_herdr_inventory"
        )
        with patch(
            module,
            return_value=_View((_Agent(), _Agent(name="foreign", workspace_id="other"))),
        ):
            observed = adapter.observe("target")
        self.assertTrue(observed.readable)
        self.assertTrue(observed.projection_complete)
        self.assertEqual(observed.live_agent_count, 1)

        with patch(module, return_value=_View((_Agent(),), raw=2, invalid=1)):
            incomplete = adapter.observe("target")
        self.assertFalse(incomplete.projection_complete)

        with patch(
            module,
            return_value=_View((_Agent(workspace_id="", managed=False),)),
        ):
            unmanaged = adapter.observe("target")
        self.assertFalse(unmanaged.projection_complete)

        with patch(module, return_value=_View((_Agent(name=""),))):
            unidentified = adapter.observe("target")
        self.assertEqual(unidentified.live_agent_count, 1)

    def test_inventory_exception_is_typed_unreadable_not_empty_success(self) -> None:
        adapter = HerdrWorkspaceRetirementInventory(
            repo_root=Path("."), env={}
        )
        module = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.workspace_retirement_inventory.read_herdr_inventory"
        )
        with patch(module, side_effect=RuntimeError("transport failed")):
            observed = adapter.observe("target")
        self.assertEqual(
            observed,
            WorkspaceRetirementInventory(
                readable=False,
                projection_complete=False,
                live_agent_count=0,
                target_agent_set_digest=observed.target_agent_set_digest,
            ),
        )


if __name__ == "__main__":
    unittest.main()
