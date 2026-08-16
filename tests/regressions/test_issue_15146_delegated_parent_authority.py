"""Redmine #15146 — delegated_coordinator creation earns its parent, or refuses.

Measured on the server-management lane: with a durable role binding declaring only
``coordinator``, ``sublane create --lane-kind delegated_coordinator`` succeeded, and
the Unit then projected three different roles on three surfaces (handoff /
route-plan / Unit board) with nothing failing closed. The lane kind is a geometry
assertion — "my parent is a project gateway" — and the admission pinned here makes
the assertion earn itself: the parent must be DECLARED (a ``project_gateway`` role
binding) and VERIFIED (that binding's derived lane owns an ACTIVE lifecycle owner
row), or the creation refuses with a typed reason before any side effect.

Both `sublane create` entry points run the SAME decision — the plan-only surface
and the --dry-run/--execute actuator — because a plan that promises a lane execute
then refuses is the plan/execute drift #14224 was filed over. The command-level
cases below drive each entry point against a real temp repo.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E402,E501
    PARENT_BINDINGS_INVALID,
    PARENT_GATEWAY_UNDECLARED,
    PARENT_GATEWAY_UNVERIFIED,
    decide_delegated_parent_authority,
    parent_authority_refusal_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E402,E501
    ParsedRoleBindings,
    parse_role_bindings,
    project_gateway_lane_id,
)


def _bindings(*entries) -> ParsedRoleBindings:
    return parse_role_bindings(
        {
            "schema": "mozyo.workflow-role-bindings",
            "version": 1,
            "bindings": list(entries),
        }
    )


GATEWAY_ENTRY = {"role": "project_gateway", "project_scope": "proj-a"}
COORDINATOR_ENTRY = {"role": "coordinator", "project_scope": "proj-a"}


class DecisionTest(unittest.TestCase):
    """The pure admission, one case per refusal and the earned pass."""

    def test_a_coordinator_only_declaration_refuses_undeclared(self) -> None:
        # The filed reproduction: only `coordinator` is bound, no gateway exists.
        verdict = decide_delegated_parent_authority(
            _bindings(COORDINATOR_ENTRY), owner_row_active=lambda lane: True
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_GATEWAY_UNDECLARED, verdict.reason)

    def test_an_empty_declaration_refuses_undeclared(self) -> None:
        verdict = decide_delegated_parent_authority(
            ParsedRoleBindings.empty(), owner_row_active=lambda lane: True
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_GATEWAY_UNDECLARED, verdict.reason)

    def test_an_invalid_declaration_refuses_outright(self) -> None:
        verdict = decide_delegated_parent_authority(
            ParsedRoleBindings.invalid("broken"),
            owner_row_active=lambda lane: True,
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_BINDINGS_INVALID, verdict.reason)

    def test_a_declared_but_unverified_gateway_refuses(self) -> None:
        # Declaration is intent; the owner row is the existing parent. Without an
        # active row the geometry is still a claim about a tier that is not there.
        verdict = decide_delegated_parent_authority(
            _bindings(GATEWAY_ENTRY), owner_row_active=lambda lane: False
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_GATEWAY_UNVERIFIED, verdict.reason)

    def test_a_declared_and_verified_gateway_admits(self) -> None:
        expected_lane = project_gateway_lane_id("proj-a")
        seen = []

        def owner_row_active(lane_id: str) -> bool:
            seen.append(lane_id)
            return True

        verdict = decide_delegated_parent_authority(
            _bindings(GATEWAY_ENTRY), owner_row_active=owner_row_active
        )

        self.assertTrue(verdict.ok)
        self.assertEqual((expected_lane,), verdict.verified_gateway_lanes)
        # The predicate was asked about the DERIVED gateway lane, not a guess.
        self.assertEqual([expected_lane], seen)

    def test_the_refusal_names_both_routes(self) -> None:
        verdict = decide_delegated_parent_authority(
            _bindings(COORDINATOR_ENTRY), owner_row_active=lambda lane: True
        )
        text = parent_authority_refusal_text(verdict)

        self.assertIn(PARENT_GATEWAY_UNDECLARED, text)
        self.assertIn("declare-project-gateway", text)
        # Close condition 3: the single-workspace route is stated, not implied.
        self.assertIn("single-workspace", text)
        self.assertIn("No worktree, pane, or dispatch was created", text)


class _TempRepo(unittest.TestCase):
    def _repo(self, bindings=None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".mozyo-bridge").mkdir()
        # A workspace anchor so `repo_scope_workspace_id` resolves — without one
        # the gate refuses on PARENT_SCOPE_UNRESOLVED before reaching the binding
        # distinctions (correct fail-closed, but not the branch under test).
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


class BothEntryPointsRefuseIdenticallyTest(_TempRepo):
    """Plan-only and dry-run/execute run the SAME admission (the #14224 lesson)."""

    def _plan_only(self, repo: Path, lane_kind: str):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_create,
        )

        args = argparse.Namespace(
            repo=str(repo),
            issue="15146",
            lane_label="issue_15146_probe",
            branch="issue_15146_probe",
            worktree="",
            journal="1",
            lane_kind=lane_kind,
            json=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_sublane_create(args)
        return code, out.getvalue() + err.getvalue()

    def _actuator(self, repo: Path, lane_kind: str):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            cmd_sublane_start,
        )

        args = argparse.Namespace(
            repo=str(repo),
            issue="15146",
            lane_label="issue_15146_probe",
            branch="issue_15146_probe",
            worktree="",
            journal="1",
            lane_kind=lane_kind,
            execute=False,
            dry_run=True,
            json=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_sublane_start(args)
        return code, out.getvalue() + err.getvalue()

    def test_the_plan_only_surface_refuses_with_the_typed_reason(self) -> None:
        code, text = self._plan_only(
            self._repo(bindings=[dict(COORDINATOR_ENTRY)]), "delegated_coordinator"
        )

        self.assertEqual(1, code)
        self.assertIn(PARENT_GATEWAY_UNDECLARED, text)
        self.assertIn("declare-project-gateway", text)

    def test_the_dry_run_actuator_refuses_before_any_side_effect(self) -> None:
        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])

        code, text = self._actuator(repo, "delegated_coordinator")

        self.assertEqual(1, code)
        self.assertIn(PARENT_GATEWAY_UNDECLARED, text)
        # Zero side effect: no worktree appeared under the repo.
        self.assertFalse((repo / ".worktrees").exists())

    def test_an_unverified_declared_gateway_refuses_too(self) -> None:
        code, text = self._plan_only(
            self._repo(bindings=[dict(GATEWAY_ENTRY)]), "delegated_coordinator"
        )

        self.assertEqual(1, code)
        self.assertIn(PARENT_GATEWAY_UNVERIFIED, text)

    def test_other_lane_kinds_never_consult_the_parent_gate(self) -> None:
        # Single-workspace setups stay untouched (close condition 3): the gate
        # returns None without reading anything for a non-delegated kind.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_refusal,
        )

        with patch(
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "workflow_role_authority_source.load_parsed_role_bindings"
        ) as loader:
            for kind in ("", "implementation", "coordinator"):
                self.assertIsNone(
                    delegated_parent_authority_refusal(self._repo(), kind)
                )
        self.assertEqual(0, loader.call_count)

    def test_an_unresolvable_workspace_scope_refuses(self) -> None:
        # No anchor, no registry: the owner row cannot be scoped, and an
        # unverifiable parent admits nothing.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_refusal,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E501
            PARENT_SCOPE_UNRESOLVED,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bare = Path(tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=bare, check=True)

        refusal = delegated_parent_authority_refusal(bare, "delegated_coordinator")

        self.assertIsNotNone(refusal)
        self.assertIn(PARENT_SCOPE_UNRESOLVED, refusal)

    def test_a_verified_gateway_admits_through_the_live_gate(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_refusal,
        )
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate as gate_module  # noqa: E501

        repo = self._repo(bindings=[dict(GATEWAY_ENTRY)])

        class _Record:
            lane_disposition = "active"

        class _Store:
            def get(self, key):
                return _Record()

        with patch(
            "mozyo_bridge.core.state.lane_lifecycle.LaneLifecycleStore",
            return_value=_Store(),
        ):
            with patch.object(gate_module, "_decide", wraps=gate_module._decide):
                refusal = delegated_parent_authority_refusal(
                    repo, "delegated_coordinator"
                )

        self.assertIsNone(refusal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
