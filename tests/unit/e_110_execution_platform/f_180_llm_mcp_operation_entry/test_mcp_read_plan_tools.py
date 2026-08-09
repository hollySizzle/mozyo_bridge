"""Read/plan handler specifications (Redmine #15161 / #15163).

Two things are pinned here:

- the handlers return **structured outcomes**, so no caller ever parses prose;
- the handlers reach shared application processing **in-process**. The acceptance
  "MCP does not depend on a CLI subprocess / stdout parse / raw tmux" is asserted
  structurally, on the parsed AST of every module in the Feature, rather than by
  observing one happy path that happened not to shell out.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application import (  # noqa: E402,E501
    read_plan_tools,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    EXECUTION_PLAN_ONLY,
    ReadPlanContext,
    ToolOutcome,
    run_docs_resolve,
    run_workflow_glance,
    run_workflow_step_plan,
)

FEATURE_ROOT = (
    ROOT
    / "src"
    / "mozyo_bridge"
    / "e_110_execution_platform"
    / "f_180_llm_mcp_operation_entry"
)

def feature_modules() -> list:
    """Every runtime module in this Feature, excluding the CLI adapter.

    ``cli_mcp`` is excluded from the *handler* rule on purpose: it is the CLI
    adapter, and the client spawning the server is the stdio transport, not a
    wrapper. It is still covered by the no-CLI-invocation assertion below.
    """
    return [p for p in FEATURE_ROOT.rglob("*.py") if p.name != "cli_mcp.py"]


class NoCliSubprocessTests(unittest.TestCase):
    def test_no_feature_module_imports_subprocess_or_os_system(self) -> None:
        offenders = []
        for path in feature_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in ("subprocess", "pty", "shlex"):
                            offenders.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] in ("subprocess", "pty", "shlex"):
                        offenders.append(f"{path.name}: from {node.module}")
        self.assertEqual(offenders, [])

    def test_no_feature_module_calls_a_process_launcher(self) -> None:
        """No module reaches ``os.system`` / ``os.exec*`` / ``os.spawn*`` / runpy.

        Checked on call sites rather than on string literals: the modules
        legitimately *contain* the string ``mozyo-bridge`` (it is the server's own
        name and appears in tool descriptions), so a text search would flag the
        server's identity as if it were an invocation.
        """
        offenders = []
        for path in FEATURE_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if not isinstance(name, str):
                    continue
                if name in ("system", "popen") or name.startswith(("exec", "spawn")):
                    offenders.append(f"{path.name}: {name}()")
                if name in ("run_module", "run_path"):
                    offenders.append(f"{path.name}: {name}()")
        self.assertEqual(offenders, [])

    def test_no_handler_invokes_the_cli_entry_point(self) -> None:
        """The CLI's ``main`` is never called to answer a tool call."""
        for path in feature_modules():
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("application.cli import", source, path.name)
            self.assertNotIn("cli.main(", source, path.name)

    def test_no_handler_reads_another_command_stdout(self) -> None:
        for path in feature_modules():
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("check_output", source, path.name)
            self.assertNotIn("Popen", source, path.name)

    def test_the_handlers_call_the_shared_application_processing(self) -> None:
        """Each handler imports the same entry point the CLI command imports."""
        source = Path(read_plan_tools.__file__).read_text(encoding="utf-8")
        self.assertIn("from mozyo_bridge.docs_tools import", source)
        self.assertIn("active_lane_snapshots", source)
        self.assertIn("fold_glance_rows", source)
        self.assertIn("resolve_workflow_step", source)


class DocsResolveTests(unittest.TestCase):
    def test_resolving_a_real_path_returns_structured_resolutions(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["src/mozyo_bridge/application/cli.py"], "include_local": True},
            ReadPlanContext(repo_root=ROOT),
        )
        self.assertIsInstance(outcome, ToolOutcome)
        self.assertFalse(outcome.is_error)
        self.assertIsInstance(outcome.payload["resolutions"], list)
        self.assertEqual(len(outcome.payload["resolutions"]), 1)
        self.assertIn("overlay_applied", outcome.payload)

    def test_an_unreadable_catalog_is_a_structured_tool_error(self) -> None:
        outcome = run_docs_resolve(
            {"paths": ["a.py"]},
            ReadPlanContext(repo_root=ROOT, catalog_path="/nonexistent/catalog.yaml"),
        )
        self.assertTrue(outcome.is_error)
        self.assertIn("error", outcome.payload)
        # Structured, not prose: the caller reads a token, not a sentence.
        self.assertIn(outcome.payload["error"], ("docs_catalog", "docs_overlay"))

    def test_the_handler_prints_nothing(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_docs_resolve({"paths": ["a.py"]}, ReadPlanContext(repo_root=ROOT))
        self.assertEqual(buffer.getvalue(), "")


class WorkflowGlanceTests(unittest.TestCase):
    def test_a_glance_with_no_reachable_source_reports_degraded_not_empty(self) -> None:
        outcome = run_workflow_glance(
            {"issues": ["15151"]},
            ReadPlanContext(repo_root=ROOT, redmine_live=False),
        )
        self.assertIn("rows", outcome.payload)
        self.assertIn("source_health", outcome.payload)
        health = outcome.payload["source_health"]
        self.assertIn("degraded", health)
        self.assertIn("notes", health)

    def test_an_unreadable_redmine_fixture_is_a_structured_tool_error(self) -> None:
        outcome = run_workflow_glance(
            {},
            ReadPlanContext(
                repo_root=ROOT, redmine_fixture_path="/nonexistent/fixture.json"
            ),
        )
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["error"], "glance_source")

    def test_the_closed_issue_partition_is_reported_separately(self) -> None:
        outcome = run_workflow_glance(
            {"issues": []}, ReadPlanContext(repo_root=ROOT, redmine_live=False)
        )
        self.assertIn("closed_coordinator_debt", outcome.payload)

    def test_the_handler_prints_nothing(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_workflow_glance(
                {"issues": ["15151"]},
                ReadPlanContext(repo_root=ROOT, redmine_live=False),
            )
        self.assertEqual(buffer.getvalue(), "")


class WorkflowStepPlanTests(unittest.TestCase):
    def test_the_plan_only_contract_holds_on_every_outcome(self) -> None:
        """Whatever the environment resolves, nothing is ever executed.

        Three outcomes are possible here depending on the host: no lane at all
        (a structured ``lane_unresolved`` refusal), a resolved-but-fail-closed
        step, or a resolved forward step. The plan-only contract is the invariant
        common to all three, so it is asserted unconditionally and the
        refusal-specific shape only where it applies.
        """
        outcome = run_workflow_step_plan({}, ReadPlanContext(repo_root=ROOT))
        self.assertEqual(outcome.payload["execution"], EXECUTION_PLAN_ONLY)
        self.assertFalse(outcome.payload["executed"])
        if outcome.payload.get("error"):
            # A lane could not be resolved: refuse, never substitute a default
            # lane, which would resolve a step for somebody else's work.
            self.assertEqual(outcome.payload["error"], "lane_unresolved")
            self.assertEqual(outcome.payload["plan"], {})
        else:
            self.assertIn("backend", outcome.payload)
            self.assertIn(outcome.payload["backend"], ("herdr", "tmux"))

    def test_the_execution_token_is_fixed_so_absence_is_never_inferred(self) -> None:
        self.assertEqual(EXECUTION_PLAN_ONLY, "plan_only")

    def test_a_journal_without_an_issue_is_reported_not_silently_dropped(self) -> None:
        notes: list = []
        anchor = read_plan_tools._anchor_from({"journal": "102124"}, notes)
        self.assertIsNone(anchor)
        self.assertTrue(notes)

    def test_an_issue_without_a_journal_matches_the_cli_anchor_rule(self) -> None:
        """The API adds no gate the CLI does not run (shared-boundary invariant 3)."""
        notes: list = []
        anchor = read_plan_tools._anchor_from({"issue": "15151"}, notes)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.issue, "15151")
        self.assertEqual(anchor.journal, "")
        self.assertEqual(notes, [])

    def test_no_anchor_arguments_yield_no_anchor(self) -> None:
        notes: list = []
        self.assertIsNone(read_plan_tools._anchor_from({}, notes))
        self.assertEqual(notes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
