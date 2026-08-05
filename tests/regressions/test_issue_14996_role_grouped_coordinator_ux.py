"""Semantic top-coordinator UX regressions for Redmine #14996.

Physical role grouping must not make pane ids a user-facing routing API. The top
coordinator already has two semantic surfaces: one explicit target repo per
``workflow proxy`` invocation, and one repeated issue roster for a read-only
``workflow glance`` aggregate. These tests pin those public command contracts;
they do not weaken ``workflow step``'s deliberate ambiguous-target refusal.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.coordinator_proxy_fence import CoordinatorProxyFence
from mozyo_bridge.core.state.workspace_registry import register_workspace
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
    render_bootstrap_decision_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    IssueExpectation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


class RoleGroupedCoordinatorSemanticUxTest(unittest.TestCase):
    def test_proxy_targets_each_project_by_repo_without_pane_address(self) -> None:
        parser = build_parser()
        parsed = []
        for repo in ("/projects/accounting", "/projects/operations"):
            args = parser.parse_args(
                [
                    "--repo",
                    repo,
                    "workflow",
                    "proxy",
                    "--action",
                    "bootstrap_lane",
                    "--issue",
                    "14996",
                    "--journal",
                    "99499",
                    "--json",
                ]
            )
            parsed.append(args)

        self.assertEqual([args.repo for args in parsed], [
            "/projects/accounting",
            "/projects/operations",
        ])
        for args in parsed:
            self.assertFalse(hasattr(args, "pane"))
            self.assertFalse(hasattr(args, "target"))
            self.assertTrue(callable(args.func))

    def test_proxy_cli_executes_once_for_each_explicit_repo(self) -> None:
        proxy_module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.coordinator_proxy_send"
        )
        active_rows = []
        sends = []

        def _orchestrate(send_args, *, default_kind):
            sends.append(send_args)
            self.assertEqual(default_kind, "custom")
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repos = [(root / name).resolve() for name in ("accounting", "operations")]
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                workspace_ids = []
                for repo in repos:
                    (repo / ".mozyo-bridge").mkdir(parents=True)
                    (repo / ".mozyo-bridge" / "workflow-role-bindings.json").write_text(
                        json.dumps(
                            {
                                "schema": SCHEMA_NAME,
                                "version": SCHEMA_VERSION,
                                "bindings": [
                                    {
                                        "role": ROLE_COORDINATOR,
                                        "project_scope": "project",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    workspace_ids.append(
                        register_workspace(repo, home=home).record.workspace_id
                    )
                CoordinatorProxyFence(home=home).bootstrap()
                with (
                    patch(f"{proxy_module}.live_agent_rows", side_effect=lambda _env: active_rows),
                    patch(
                        f"{proxy_module}.live_named_journal_note",
                        return_value=(render_bootstrap_decision_marker(), True),
                    ),
                    patch(
                        f"{proxy_module}.live_attestation_join",
                        return_value=(True, "present", "generation matched"),
                    ),
                    patch(
                        f"{proxy_module}.live_issue_expectation",
                        side_effect=lambda _repo, issue, _decisions, action="": (
                            IssueExpectation(
                                issue=issue,
                                owns_active_lane=False,
                                latest_decision_journal="",
                            )
                        ),
                    ),
                    patch(
                        "mozyo_bridge.application.commands.orchestrate_handoff",
                        side_effect=_orchestrate,
                    ),
                ):
                    for index, (repo, workspace_id) in enumerate(
                        zip(repos, workspace_ids), start=1
                    ):
                        active_rows[:] = [
                            {
                                "name": encode_assigned_name(workspace_id, "codex", ""),
                                "pane_id": f"wProjects:p{index}",
                            }
                        ]
                        args = build_parser().parse_args(
                            [
                                "--repo",
                                str(repo),
                                "workflow",
                                "proxy",
                                "--action",
                                "bootstrap_lane",
                                "--issue",
                                "14996",
                                "--journal",
                                "99499",
                                "--execute",
                                "--json",
                            ]
                        )
                        out = io.StringIO()
                        with contextlib.redirect_stdout(out):
                            rc = args.func(args)
                        self.assertEqual(rc, 0)
                        payload = json.loads(out.getvalue())
                        self.assertTrue(payload["sent"])
                        self.assertEqual(payload["workspace_id"], workspace_id)
                        self.assertEqual(payload["lane_id"], "default")
                        self.assertFalse(hasattr(args, "pane"))

        self.assertEqual(len(sends), 2)
        self.assertEqual(
            [Path(send.target_repo) for send in sends],
            repos,
        )
        self.assertEqual([send.target_lane for send in sends], ["default", "default"])

    def test_glance_aggregates_two_explicit_issues_in_one_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "glance.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "issues": [
                            {"issue": "14996", "subject": "accounting coordinator"},
                            {"issue": "14997", "subject": "operations coordinator"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "workflow",
                    "glance",
                    "--snapshot-json",
                    str(snapshot),
                    "--no-ledger",
                    "--json",
                    "--issue",
                    "14996",
                    "--issue",
                    "14997",
                ]
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = args.func(args)

        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [row["issue_id"] for row in payload["rows"]],
            ["14996", "14997"],
        )


if __name__ == "__main__":
    unittest.main()
