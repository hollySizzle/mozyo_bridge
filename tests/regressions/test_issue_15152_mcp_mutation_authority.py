"""Redmine #15152 — mutating MCP tools run the CLI's durable authority gates.

The acceptance pinned here, one class per claim:

- the MCP ``sublane_start`` tool runs the SAME #15146 parent-authority
  admission the CLI runs — a delegated_coordinator without a durably declared
  AND verified parent project gateway refuses with the typed verdict reason,
  before any worktree / pair / dispatch side effect, on the plan and on the
  actuate path alike. The MCP adapter is not a workaround for #15146: both
  entries call the one shared service body, and the CLI handler no longer
  carries a gate of its own to drift from.
- the MCP ``handoff_send`` tool rides the shared orchestration's gates: the
  same invalid input refuses through the typed API and through the tool, with
  nothing sent.
- the mutating input schemas cannot express a pane locator / tmux target /
  command string, which is the structural half of refusing the unmanaged
  (identity-less) rows measured in j#102930 / j#102998 — a row that durable
  authority cannot name cannot be a mutation target here.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E402,E501
    PARENT_GATEWAY_UNDECLARED,
    PARENT_GATEWAY_UNVERIFIED,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.tool_dispatch import (  # noqa: E402,E501
    dispatch_tool_call,
)


class _TempRepo(unittest.TestCase):
    def _repo(self, bindings=None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": "a" * 32,
                    "canonical_session": "mozyo-test",
                    "project_name": "test",
                    "created_at": "2026-08-16T00:00:00+00:00",
                    "updated_at": "2026-08-16T00:00:00+00:00",
                }
            )
        )
        if bindings is not None:
            (repo / ".mozyo-bridge" / "workflow-role-bindings.json").write_text(
                json.dumps(
                    {
                        "schema": "mozyo.workflow-role-bindings",
                        "version": 1,
                        "bindings": bindings,
                    }
                )
            )
        return repo

    def _sublane_start(self, repo: Path, *, actuate: bool):
        return dispatch_tool_call(
            "sublane_start",
            {
                "issue": "15152",
                "lane_label": "issue_15152_probe",
                "branch": "issue_15152_probe",
                "lane_kind": "delegated_coordinator",
                "actuate": actuate,
            },
            ReadPlanContext(repo_root=repo),
        )


COORDINATOR_ONLY = [{"role": "coordinator", "project_scope": "proj-a"}]
GATEWAY_DECLARED = [{"role": "project_gateway", "project_scope": "proj-a"}]


class McpSublaneParentAuthorityTest(_TempRepo):
    """The #15146 admission fires identically through the MCP tool."""

    def test_the_plan_refuses_undeclared_with_zero_side_effect(self) -> None:
        repo = self._repo(bindings=COORDINATOR_ONLY)

        dispatched = self._sublane_start(repo, actuate=False)

        result = dispatched.result
        self.assertTrue(result["isError"])
        content = result["structuredContent"]
        self.assertEqual("refused", content["status"])
        self.assertEqual(PARENT_GATEWAY_UNDECLARED, content["refusal_reason"])
        self.assertFalse(content["executed"])
        self.assertFalse((repo / ".worktrees").exists())

    def test_the_actuate_path_refuses_identically(self) -> None:
        repo = self._repo(bindings=COORDINATOR_ONLY)

        dispatched = self._sublane_start(repo, actuate=True)

        content = dispatched.result["structuredContent"]
        self.assertEqual(PARENT_GATEWAY_UNDECLARED, content["refusal_reason"])
        self.assertFalse(content["executed"])
        self.assertFalse((repo / ".worktrees").exists())

    def test_a_declared_but_unverified_gateway_refuses_unverified(self) -> None:
        repo = self._repo(bindings=GATEWAY_DECLARED)

        dispatched = self._sublane_start(repo, actuate=False)

        content = dispatched.result["structuredContent"]
        self.assertEqual(PARENT_GATEWAY_UNVERIFIED, content["refusal_reason"])

    def test_the_cli_refuses_with_the_same_typed_reason(self) -> None:
        # Same repo, same lane kind, driven through the CLI actuation entry: the
        # refusal token is byte-identical because both entries run ONE body.
        import argparse
        import contextlib
        import io

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            cmd_sublane_start,
        )

        repo = self._repo(bindings=COORDINATOR_ONLY)
        args = argparse.Namespace(
            repo=str(repo),
            issue="15152",
            lane_label="issue_15152_probe",
            branch="issue_15152_probe",
            worktree="",
            journal="1",
            lane_kind="delegated_coordinator",
            execute=False,
            dry_run=True,
            json=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_sublane_start(args)

        self.assertEqual(1, code)
        self.assertIn(PARENT_GATEWAY_UNDECLARED, out.getvalue() + err.getvalue())

        mcp = self._sublane_start(repo, actuate=False)
        self.assertEqual(
            PARENT_GATEWAY_UNDECLARED,
            mcp.result["structuredContent"]["refusal_reason"],
        )


class OneSharedBodyTest(unittest.TestCase):
    """The CLI actuation handler no longer carries its own gate chain to drift.

    j#84882 / #14224: two restatements of the same admissions is how plan and
    execute drifted. The structural assertion: ``cmd_sublane_start`` calls the
    shared ``run_sublane_start`` and does NOT call the admission gates
    directly; the service module is where they live.
    """

    def _function_calls(self, module_path: Path, function: str) -> set:
        tree = ast.parse(module_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                return {
                    n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                }
        raise AssertionError(f"{function} not found in {module_path}")

    def test_cmd_sublane_start_routes_through_the_shared_service(self) -> None:
        module = (
            ROOT
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_140_delegated_coordinator_nested_handoff/application"
            / "sublane_actuator.py"
        )
        calls = self._function_calls(module, "cmd_sublane_start")
        self.assertIn("run_sublane_start", calls)
        for gate in (
            "delegated_parent_authority_refusal",
            "_sublane_start_provider_preflight_blocked",
            "resolve_work_unit_request_fields",
        ):
            self.assertNotIn(gate, calls, gate)

    def test_the_mcp_handler_routes_through_the_same_service(self) -> None:
        module = (
            ROOT
            / "src/mozyo_bridge/e_110_execution_platform"
            / "f_180_llm_mcp_operation_entry/application/mutation_tools.py"
        )
        calls = self._function_calls(module, "run_sublane_start_tool")
        self.assertIn("run_sublane_start", calls)


class McpHandoffSharedGateTest(unittest.TestCase):
    """The handoff tool rides the shared orchestration's own gates."""

    def _context(self) -> ReadPlanContext:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ReadPlanContext(repo_root=Path(tmp.name).resolve())

    def test_an_unknown_receiver_refuses_with_nothing_sent(self) -> None:
        # The receiver-vocabulary gate is deterministic and pre-side-effect
        # (the probe the #15149 CLI/API parity tests chose for the same
        # reason). Through the MCP tool it must refuse fail-closed — and the
        # typed API must refuse the SAME input the same way, because the tool
        # is an entry over `run_handoff`, not a second orchestration.
        dispatched = dispatch_tool_call(
            "handoff_send",
            {"to": "martian", "source": "redmine", "issue": "1", "journal": "2"},
            self._context(),
        )

        result = dispatched.result
        self.assertTrue(result["isError"])
        content = result["structuredContent"]
        self.assertEqual("fail_closed", content["status"])
        self.assertFalse(content["delivered"])

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E501
            HandoffRequest,
            run_handoff,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (  # noqa: E501
            HandoffCommandInput,
        )

        api = run_handoff(
            HandoffRequest(
                operation="send",
                input=HandoffCommandInput(
                    to="martian", source="redmine", issue="1", journal="2"
                ),
                repo_root=Path("."),
            )
        )
        self.assertTrue(api.fail_closed)
        self.assertEqual(api.exit_code, content["exit_code"])

    def test_the_gate_prose_never_reaches_the_tool_payload(self) -> None:
        dispatched = dispatch_tool_call(
            "handoff_send",
            {"to": "martian", "source": "redmine", "issue": "1", "journal": "2"},
            self._context(),
        )

        content = dispatched.result["structuredContent"]
        # The refusal member is the fixed sentence, not the CLI `die` text.
        self.assertIn("shared-orchestration gate refused", content["refusal"])
        self.assertNotIn("martian", content["refusal"])


class R2PreMutationDurableAuthorityTest(_TempRepo):
    """R2 (review j#106834 finding_authoritybypass): every MCP actuation mode
    verifies the durable anchor and the caller's sender authority BEFORE any
    workspace / lane / pair mutation.

    R1 shipped `actuate=true, dispatch=false` riding the use case's old
    `execute and dispatch` gate scope — a create-only run mutated the workspace
    with no journal and no sender attestation, and even a dispatching run only
    checked the journal for non-emptiness before mutating (ownership was
    verified inside the dispatch handoff, after the lane existed). Pinned here
    through the REAL dispatch path for both dispatch values: missing anchor,
    unreadable / not-found / mismatched anchor, and an unattested sender each
    refuse typed with zero worktree / pair / dispatch calls.
    """

    def _fake_ops_cls(self, *, sender_ok=True):
        from tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_sublane_actuator import (  # noqa: E501
            FakeActuatorOps,
            _lane,
        )

        class _Ops(FakeActuatorOps):
            def __init__(self):
                super().__init__(git=True, lanes=[None, _lane()])

            def preflight_dispatch_sender(self):
                return (True, "sender_attested") if sender_ok else (
                    False,
                    "sender_workspace_mismatch: resolved != anchor",
                )

        return _Ops

    def _run(self, repo, *, dispatch, journal, ops_cls, verify=None):
        from unittest.mock import patch

        import mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_anchor_authority as anchor_authority  # noqa: E501
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        ops = ops_cls()
        arguments = {
            "issue": "15152",
            "lane_label": "issue_15152_probe",
            "branch": "issue_15152_probe",
            "worktree": "/wt/issue_15152_probe",
            "actuate": True,
            "dispatch": dispatch,
        }
        if journal:
            arguments["journal"] = journal
        with patch.object(
            anchor_authority,
            "verify_live_handoff_anchor",
            verify if verify is not None else (lambda anchor: anchor),
        ):
            with patch.object(act, "_resolve_sublane_ops", return_value=ops) as resolver:
                dispatched = dispatch_tool_call(
                    "sublane_start", arguments, ReadPlanContext(repo_root=repo)
                )
        return dispatched, ops, resolver

    def _mutations(self, ops):
        names = [c[0] if isinstance(c, tuple) else c for c in ops.calls]
        return [
            n
            for n in names
            if n in ("create_worktree", "append_lane_column", "dispatch")
        ]

    def test_a_missing_journal_refuses_both_dispatch_modes(self) -> None:
        for dispatch in (True, False):
            with self.subTest(dispatch=dispatch):
                dispatched, ops, _ = self._run(
                    self._repo(),
                    dispatch=dispatch,
                    journal=None,
                    ops_cls=self._fake_ops_cls(),
                )
                content = dispatched.result["structuredContent"]
                self.assertTrue(dispatched.result["isError"])
                self.assertIn(
                    "anchor_required", content["outcome"]["blocked_reasons"]
                )
                self.assertEqual([], self._mutations(ops))

    def test_an_unowned_or_unreadable_anchor_refuses_before_ops(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_anchor_authority import (  # noqa: E501
            AnchorAuthorityError,
        )

        for reason in (
            "anchor_issue_not_found",
            "anchor_journal_not_found",
            "anchor_issue_journal_mismatch",
            "anchor_provider_unreadable",
        ):
            for dispatch in (True, False):
                with self.subTest(reason=reason, dispatch=dispatch):

                    def _refuse(anchor, _reason=reason):
                        raise AnchorAuthorityError(_reason, "refused", anchor)

                    dispatched, ops, resolver = self._run(
                        self._repo(),
                        dispatch=dispatch,
                        journal="999",
                        ops_cls=self._fake_ops_cls(),
                        verify=_refuse,
                    )
                    content = dispatched.result["structuredContent"]
                    self.assertTrue(dispatched.result["isError"])
                    self.assertEqual("refused", content["status"])
                    self.assertEqual(reason, content["refusal_reason"])
                    # Refused in the service admission, strictly before the
                    # actuation ops even exist.
                    self.assertEqual(0, resolver.call_count)
                    self.assertEqual([], self._mutations(ops))

    def test_an_unattested_sender_refuses_both_dispatch_modes(self) -> None:
        for dispatch in (True, False):
            with self.subTest(dispatch=dispatch):
                dispatched, ops, _ = self._run(
                    self._repo(),
                    dispatch=dispatch,
                    journal="999",
                    ops_cls=self._fake_ops_cls(sender_ok=False),
                )
                content = dispatched.result["structuredContent"]
                self.assertTrue(dispatched.result["isError"])
                self.assertIn(
                    "sender_attestation", content["outcome"]["blocked_reasons"]
                )
                self.assertEqual([], self._mutations(ops))


class NoRawSurfaceExpressibleTest(unittest.TestCase):
    """j#102930 / j#102998: the mutating surface cannot address unmanaged rows.

    An unmanaged pane has no assigned identity and no durable authority row;
    the only way to reach it is a raw pane locator or tmux target. Those are
    not representable in the mutating input schemas, and a caller trying to
    smuggle one in is refused at the schema boundary, before any handler runs.
    """

    def test_a_pane_locator_argument_is_a_protocol_error(self) -> None:
        for name, base in (
            ("handoff_send", {"to": "codex"}),
            ("handoff_reply", {"to": "claude"}),
            ("sublane_start", {"issue": "1", "lane_label": "x"}),
        ):
            dispatched = dispatch_tool_call(
                name,
                {**base, "target": "%5"},
                ReadPlanContext(repo_root=Path(".")),
            )
            self.assertTrue(dispatched.is_protocol_error, name)
            self.assertTrue(
                any(
                    "unknown property" in v
                    for v in dispatched.protocol_error.data["violations"]
                ),
                name,
            )

    def test_launch_goes_only_through_the_managed_creator_rail(self) -> None:
        # Structural: the sublane tool's mutation body is the shared service,
        # whose actuation adapters are the managed creator rails (herdr
        # session rail / cockpit append) that assign durable identity. No
        # module in the MCP feature composes a pane split, a tmux call, or a
        # subprocess of its own.
        feature = (
            ROOT / "src/mozyo_bridge/e_110_execution_platform/f_180_llm_mcp_operation_entry"
        )
        for path in feature.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names]
                    module = getattr(node, "module", "") or ""
                    self.assertNotIn("subprocess", names, path.name)
                    self.assertFalse(module.startswith("subprocess"), path.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
