"""``workflow role-authority`` mint / readback surface tests (Redmine #14546).

Pins the public surface a bare ``mozyo`` workspace needs before ``workflow step`` can resolve its
default lane: the fail-closed, readback-verified mint of the single-workspace ``coordinator``
binding, and the layered readback a restart / adopt recovers from.

The two properties that matter most here are the ones a convenience implementation would quietly
drop: (a) the mint **never overwrites an authority it did not author** — a present-but-unparseable
declaration and an already-bound default lane both fail closed rather than being replaced, and
(b) a write is only reported as success after the file is **re-read and re-parsed** into the
intended binding, so a write that lands as something else is a failure, not a success.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (  # noqa: E501
    ROLE_GRANDPARENT_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_role_authority import (  # noqa: E501
    MINT_ALREADY_DECLARED,
    MINT_BLOCKED_DECLARATION_INVALID,
    MINT_BLOCKED_LANE_BOUND,
    MINT_BLOCKED_SCOPE_REQUIRED,
    MINT_DECLARED,
    cmd_workflow_role_authority,
    plan_coordinator_mint,
    resolve_role_authority_status,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_role_authority_source import (  # noqa: E501
    load_parsed_role_bindings,
    role_bindings_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
    DEFAULT_LANE,
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (  # noqa: E501
    ROLE_COORDINATOR,
)

SCOPE = "bare_mozyo_workspace"


class RoleAuthorityCliTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            repo=str(self.repo),
            mint_coordinator=False,
            project_scope="",
            source_pointer="",
            execute=False,
            as_json=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, **overrides) -> "tuple[int, dict]":
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_workflow_role_authority(self._args(**overrides))
        return rc, json.loads(buf.getvalue())

    def _write(self, record) -> None:
        role_bindings_path(self.repo).write_text(
            json.dumps(record) if not isinstance(record, str) else record, encoding="utf-8"
        )


class MintTest(RoleAuthorityCliTestBase):
    def test_mint_requires_a_project_scope(self):
        rc, out = self._run(mint_coordinator=True, execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"], MINT_BLOCKED_SCOPE_REQUIRED)
        self.assertFalse(role_bindings_path(self.repo).exists())

    def test_dry_run_writes_nothing(self):
        rc, out = self._run(mint_coordinator=True, project_scope=SCOPE)
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], MINT_DECLARED)
        self.assertFalse(out["executed"])
        self.assertFalse(role_bindings_path(self.repo).exists())

    def test_execute_declares_and_reads_back(self):
        rc, out = self._run(
            mint_coordinator=True,
            project_scope=SCOPE,
            source_pointer="redmine:#14546",
            execute=True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], MINT_DECLARED)
        self.assertTrue(out["executed"])

        record = json.loads(role_bindings_path(self.repo).read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], SCHEMA_NAME)
        self.assertEqual(record["version"], SCHEMA_VERSION)
        self.assertEqual(
            record["bindings"],
            [
                {
                    "role": ROLE_COORDINATOR,
                    "project_scope": SCOPE,
                    "source_pointer": "redmine:#14546",
                }
            ],
        )
        # The lane id is DERIVED, never stored (the #13583 storage boundary is preserved).
        self.assertNotIn("lane_id", record["bindings"][0])
        # No workspace id / liveness / provider leaked into the static artifact.
        for forbidden in ("workspace_id", "provider", "locator", "status"):
            self.assertNotIn(forbidden, record["bindings"][0])

        parsed = load_parsed_role_bindings(self.repo)
        self.assertTrue(parsed.ok)
        (binding,) = parsed.bindings
        self.assertEqual(binding.role, ROLE_COORDINATOR)
        self.assertEqual(binding.lane_id, DEFAULT_LANE)

    def test_re_mint_is_idempotent_and_preserves_the_existing_pointer(self):
        self._run(
            mint_coordinator=True, project_scope=SCOPE,
            source_pointer="redmine:#14546", execute=True,
        )
        before = role_bindings_path(self.repo).read_text(encoding="utf-8")
        rc, out = self._run(
            mint_coordinator=True, project_scope=SCOPE,
            source_pointer="redmine:#99999", execute=True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], MINT_ALREADY_DECLARED)
        self.assertFalse(out["executed"])
        # Byte-identical: an idempotent re-mint is a no-op, not a silent pointer rewrite.
        self.assertEqual(role_bindings_path(self.repo).read_text(encoding="utf-8"), before)

    def test_a_different_scope_is_blocked_not_overwritten(self):
        self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        before = role_bindings_path(self.repo).read_text(encoding="utf-8")
        rc, out = self._run(mint_coordinator=True, project_scope="other", execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"], MINT_BLOCKED_LANE_BOUND)
        self.assertEqual(role_bindings_path(self.repo).read_text(encoding="utf-8"), before)

    def test_a_bound_grandparent_default_lane_is_blocked_not_overwritten(self):
        self._write(
            {
                "schema": SCHEMA_NAME,
                "version": SCHEMA_VERSION,
                "bindings": [{"role": ROLE_GRANDPARENT_COORDINATOR}],
            }
        )
        before = role_bindings_path(self.repo).read_text(encoding="utf-8")
        rc, out = self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"], MINT_BLOCKED_LANE_BOUND)
        self.assertIn(ROLE_GRANDPARENT_COORDINATOR, out["detail"])
        self.assertEqual(role_bindings_path(self.repo).read_text(encoding="utf-8"), before)

    def test_an_unparseable_declaration_is_never_clobbered(self):
        self._write("{ this is not json")
        before = role_bindings_path(self.repo).read_text(encoding="utf-8")
        rc, out = self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out["outcome"], MINT_BLOCKED_DECLARATION_INVALID)
        self.assertEqual(role_bindings_path(self.repo).read_text(encoding="utf-8"), before)

    def test_a_valid_gateway_declaration_is_preserved_by_the_mint(self):
        self._write(
            {
                "schema": SCHEMA_NAME,
                "version": SCHEMA_VERSION,
                "bindings": [{"role": "project_gateway", "project_scope": "other_project"}],
            }
        )
        rc, out = self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], MINT_DECLARED)
        parsed = load_parsed_role_bindings(self.repo)
        self.assertTrue(parsed.ok)
        roles = sorted(b.role for b in parsed.bindings)
        self.assertEqual(roles, [ROLE_COORDINATOR, "project_gateway"])


class ReadbackTest(RoleAuthorityCliTestBase):
    def test_absent_declaration_reads_back_as_absent_not_as_an_error_state(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertFalse(out["declaration"]["present"])
        self.assertTrue(out["declaration"]["parsed_ok"])
        self.assertEqual(out["declaration"]["bindings"], [])
        self.assertEqual(out["default_lane_role"], "")

    def test_invalid_declaration_reads_back_nonzero_with_its_reason(self):
        self._write("{ nope")
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertTrue(out["declaration"]["present"])
        self.assertFalse(out["declaration"]["parsed_ok"])
        self.assertTrue(out["declaration"]["reason"])

    def test_declared_coordinator_reads_back_with_its_expected_provider(self):
        self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out["default_lane_role"], ROLE_COORDINATOR)
        self.assertEqual(out["default_lane_project_scope"], SCOPE)
        self.assertEqual(out["expected_provider"]["binding_role"], ROLE_COORDINATOR)
        self.assertEqual(out["expected_provider"]["status"], "resolved")

    def test_readback_reports_each_recovery_layer_separately(self):
        # A restart has to re-establish four independent things. Collapsing them is what made
        # the original failure unreadable, so the envelope must keep them apart.
        status = resolve_role_authority_status(self.repo)
        for layer in (
            "declaration",
            "workspace_anchor",
            "expected_provider",
            "live_attestation",
        ):
            self.assertIn(layer, status)

    def test_readback_never_leaks_an_absolute_repo_path(self):
        self._run(mint_coordinator=True, project_scope=SCOPE, execute=True)
        _rc, out = self._run()
        blob = json.dumps(out)
        self.assertNotIn(str(self.repo), blob)
        self.assertEqual(out["declaration"]["path"], ".mozyo-bridge/workflow-role-bindings.json")


class PlanPurityTest(unittest.TestCase):
    def test_plan_is_pure_over_the_parsed_declaration(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
            parse_role_bindings,
        )

        plan = plan_coordinator_mint(
            parse_role_bindings(None), project_scope=SCOPE, source_pointer="redmine:#14546"
        )
        self.assertEqual(plan["outcome"], MINT_DECLARED)
        self.assertEqual(
            plan["record"],
            {
                "schema": SCHEMA_NAME,
                "version": SCHEMA_VERSION,
                "bindings": [
                    {
                        "role": ROLE_COORDINATOR,
                        "project_scope": SCOPE,
                        "source_pointer": "redmine:#14546",
                    }
                ],
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
