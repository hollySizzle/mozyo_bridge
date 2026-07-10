"""herdr-native `workflow step` application adapter tests (Redmine #13489).

Hermetic: the terminal-runtime seams (repo root, sender identity, project scope, live
inventory) are patched so no test depends on a repo-local config, the workspace registry, or
a live herdr binary. Pins that the adapter (a) fails closed on an unattested identity, (b)
reads the inventory ONLY for a gateway lane (worker / coordinator resolve from env alone),
and (c) folds the live inventory through the real assigned-name decode for worker liveness.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    herdr_workflow_step as adapter,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    REASON_HERDR_COORDINATOR_ORCHESTRATION,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    REASON_HERDR_WORKER_DISPATCH_READY,
    REASON_HERDR_WORKER_STEP_READY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain import (
    herdr_target_resolution as htr,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"


def _sender_ok(role, lane):
    return htr.SenderIdentityResolution.success(
        htr.SenderIdentity(workspace_id=WS, role=role, lane_id=lane)
    )


class ResolveHerdrStepOutcomeTest(unittest.TestCase):
    def setUp(self):
        # Common seams: repo root + anchor workspace are stubbed so no anchor / registry read
        # happens. Each test patches `resolve_sender_identity` for its lane role.
        # `repo_root_from_args` is a lazy import from commands_common inside the adapter;
        # patch it at the source module.
        from mozyo_bridge.application import commands_common

        self._repo_patch = patch.object(
            commands_common, "repo_root_from_args", return_value=Path("/repo")
        )
        self._anchor_patch = patch.object(
            adapter, "_anchor_workspace_id", return_value=WS
        )
        self._repo_patch.start()
        self._anchor_patch.start()
        self.addCleanup(self._repo_patch.stop)
        self.addCleanup(self._anchor_patch.stop)

    def _run(self):
        return adapter.resolve_herdr_step_outcome(argparse.Namespace(repo=None))

    def test_missing_env_fails_closed_sender_identity_unresolved(self):
        with patch.object(
            htr,
            "resolve_sender_identity",
            return_value=htr.SenderIdentityResolution.failure(
                htr.REASON_MISSING_SENDER_ENV, "unset"
            ),
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_SENDER_IDENTITY_UNRESOLVED)
        self.assertEqual(out.execution, "blocked")
        self.assertEqual(out.next_owner, "operator")
        self.assertIn("missing_sender_env", out.detail)

    def test_worker_lane_resolves_without_inventory_or_scope_read(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("claude", "issue_1")
        ), patch.object(
            adapter, "_project_scope_for", side_effect=AssertionError("scope read for worker")
        ), patch.object(
            adapter,
            "_same_lane_worker_liveness",
            side_effect=AssertionError("inventory read for worker"),
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_STEP_READY)
        self.assertEqual(out.next_owner, "grandchild")

    def test_gateway_lane_reads_inventory_for_worker_liveness(self):
        seen = {}

        def _liveness(ws, lane, *, env):
            seen["args"] = (ws, lane)
            return True

        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "issue_1")
        ), patch.object(
            adapter, "_project_scope_for", return_value=""
        ), patch.object(
            adapter, "_same_lane_worker_liveness", side_effect=_liveness
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_WORKER_DISPATCH_READY)
        self.assertEqual(seen["args"], (WS, "issue_1"))

    def test_coordinator_lane_skips_inventory(self):
        with patch.object(
            htr, "resolve_sender_identity", return_value=_sender_ok("codex", "default")
        ), patch.object(
            adapter, "_project_scope_for", return_value="mozyo_bridge"
        ), patch.object(
            adapter,
            "_same_lane_worker_liveness",
            side_effect=AssertionError("inventory read for coordinator"),
        ):
            out = self._run()
        self.assertEqual(out.reason, REASON_HERDR_COORDINATOR_ORCHESTRATION)
        self.assertEqual(out.caller_role, "project_gateway")


class SameLaneWorkerLivenessTest(unittest.TestCase):
    """The inventory fold for a gateway's same-lane worker (real assigned-name decode)."""

    def _rows(self, *specs):
        rows = []
        for role, lane in specs:
            rows.append(
                {AGENT_KEY_NAME: encode_assigned_name(WS, role, lane), AGENT_KEY_LOCATOR: "p1"}
            )
        return rows

    def test_live_same_lane_worker_is_true(self):
        # `list_herdr_agent_rows` is a lazy import inside the adapter; patch at its source.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        with patch.object(
            sublane_herdr_projection,
            "list_herdr_agent_rows",
            return_value=self._rows(("claude", "issue_1"), ("codex", "issue_1")),
        ):
            self.assertIs(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), True
            )

    def test_no_worker_in_lane_is_false(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )

        with patch.object(
            sublane_herdr_projection,
            "list_herdr_agent_rows",
            return_value=self._rows(("claude", "other_lane"), ("codex", "issue_1")),
        ):
            self.assertIs(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={}), False
            )

    def test_inventory_unavailable_is_none(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_herdr_projection,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            HerdrSessionStartError,
        )

        with patch.object(
            sublane_herdr_projection,
            "list_herdr_agent_rows",
            side_effect=HerdrSessionStartError("herdr down"),
        ):
            self.assertIsNone(
                adapter._same_lane_worker_liveness(WS, "issue_1", env={})
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
