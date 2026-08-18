"""Redmine #15700 — a single workspace earns a delegated_coordinator lane.

The #15146 admission made ``--lane-kind delegated_coordinator`` assert a parent
project gateway, which a single workspace (a project-subdir anchor with no monorepo
tier) can never establish — measured as ``parent_gateway_binding_missing`` on
#15693 (j#107824). The coordinator decision (#15693 j#107868, option b) adds a
single-workspace path WITHOUT weakening the monorepo claim: when NO
``project_gateway`` binding is declared at all, the lane's parent is the
workspace's own default-lane coordinator, and it must be DECLARED (``coordinator``
bound, provider bound) and LIVE (exactly one live attested default-lane occupant —
the coordinator proxy rail's own exactly-one policy). Every gap refuses typed
before any side effect, and the taken branch is observable (``parent_kind``),
never a silent fallback.

Pinned here:

- the coordinator branch's admission and each typed refusal (pure domain);
- the declared-gateway topology NEVER consults the coordinator probe — declared
  and verified admits as before, declared and unverified refuses as before, even
  with a perfectly live coordinator (no silent fallback, either direction);
- the live gate composition admits through both entry points with the probe's
  reads patched, and surfaces ``parent_kind=default_lane_coordinator``;
- the L2 rails this lane hangs from are the EXISTING ones (#15700 design D4): the
  coordinator's forward leg to delegated_coordinator, the fixed L2->L3 role
  profile chain, and the ``coordinator`` callback route identity.
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
    PARENT_AUTHORITY_UNDECLARED,
    PARENT_COORDINATOR_BLOCKED,
    PARENT_COORDINATOR_LIVE_AMBIGUOUS,
    PARENT_COORDINATOR_LIVE_MISSING,
    PARENT_COORDINATOR_LOCATOR_MISSING,
    PARENT_COORDINATOR_PROVIDER_UNRESOLVED,
    PARENT_COORDINATOR_ROLE_MISMATCH,
    PARENT_COORDINATOR_UNATTESTED,
    PARENT_GATEWAY_UNVERIFIED,
    PARENT_KIND_DEFAULT_COORDINATOR,
    PARENT_KIND_PROJECT_GATEWAY,
    CoordinatorParentProbe,
    decide_delegated_parent_authority,
    parent_authority_refusal_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E402,E501
    parse_role_bindings,
)


def _bindings(*entries):
    return parse_role_bindings(
        {
            "schema": "mozyo.workflow-role-bindings",
            "version": 1,
            "bindings": list(entries),
        }
    )


GATEWAY_ENTRY = {"role": "project_gateway", "project_scope": "proj-a"}
COORDINATOR_ENTRY = {"role": "coordinator", "project_scope": "proj-a"}

OK_PROBE = CoordinatorParentProbe(
    authority_status="resolved",
    role="coordinator",
    provider="codex",
    live_status="ok",
    verified_target="mzb1-attested-coordinator",
)


class CoordinatorBranchDecisionTest(unittest.TestCase):
    """The pure single-workspace branch: the earned pass and every typed refusal."""

    def _decide(self, probe, bindings=None):
        parsed = _bindings(COORDINATOR_ENTRY) if bindings is None else bindings
        return decide_delegated_parent_authority(
            parsed,
            owner_row_active=lambda lane, scope: False,
            coordinator_parent_probe=lambda: probe,
        )

    def test_a_live_attested_coordinator_parent_admits(self) -> None:
        verdict = self._decide(OK_PROBE)

        self.assertTrue(verdict.ok)
        self.assertEqual(PARENT_KIND_DEFAULT_COORDINATOR, verdict.parent_kind)
        self.assertEqual("mzb1-attested-coordinator", verdict.verified_coordinator)

    def test_a_fully_undeclared_workspace_refuses_undeclared(self) -> None:
        verdict = self._decide(
            CoordinatorParentProbe(authority_status="missing"),
            bindings=_bindings(),
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_AUTHORITY_UNDECLARED, verdict.reason)
        self.assertEqual(PARENT_KIND_DEFAULT_COORDINATOR, verdict.parent_kind)

    def test_a_blocked_authority_refuses_blocked(self) -> None:
        verdict = self._decide(
            CoordinatorParentProbe(
                authority_status="blocked",
                authority_reason="herdr_role_binding_invalid",
            )
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_BLOCKED, verdict.reason)
        self.assertIn("herdr_role_binding_invalid", verdict.detail)

    def test_a_grandparent_bound_default_lane_refuses_role_mismatch(self) -> None:
        # A monorepo root's default lane is not the single-workspace parent; that
        # topology establishes its tier via declare-project-gateway instead.
        verdict = self._decide(
            CoordinatorParentProbe(
                authority_status="resolved", role="grandparent_coordinator"
            )
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_ROLE_MISMATCH, verdict.reason)
        self.assertIn("declare-project-gateway", verdict.detail)

    def test_an_unbound_provider_refuses(self) -> None:
        verdict = self._decide(
            CoordinatorParentProbe(
                authority_status="resolved", role="coordinator", provider=""
            )
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_PROVIDER_UNRESOLVED, verdict.reason)

    def test_each_live_gap_maps_to_its_own_typed_refusal(self) -> None:
        # The mechanical TARGET_* -> refusal map, one token per liveness gap; an
        # unrecognized status folds to BLOCKED (an unknown liveness verifies
        # nothing).
        cases = {
            "missing": PARENT_COORDINATOR_LIVE_MISSING,
            "ambiguous": PARENT_COORDINATOR_LIVE_AMBIGUOUS,
            "locator_missing": PARENT_COORDINATOR_LOCATOR_MISSING,
            "unattested": PARENT_COORDINATOR_UNATTESTED,
            "something-new": PARENT_COORDINATOR_BLOCKED,
        }
        for live_status, expected in cases.items():
            with self.subTest(live_status=live_status):
                verdict = self._decide(
                    CoordinatorParentProbe(
                        authority_status="resolved",
                        role="coordinator",
                        provider="codex",
                        live_status=live_status,
                        live_detail="live=0 with_locator=0",
                    )
                )

                self.assertFalse(verdict.ok)
                self.assertEqual(expected, verdict.reason)

    def test_no_probe_fails_closed(self) -> None:
        verdict = decide_delegated_parent_authority(
            _bindings(COORDINATOR_ENTRY),
            owner_row_active=lambda lane, scope: False,
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_BLOCKED, verdict.reason)

    def test_the_coordinator_refusal_names_the_branch_and_both_remedies(self) -> None:
        verdict = self._decide(
            CoordinatorParentProbe(
                authority_status="resolved",
                role="coordinator",
                provider="codex",
                live_status="missing",
            )
        )
        text = parent_authority_refusal_text(verdict)

        self.assertIn(PARENT_COORDINATOR_LIVE_MISSING, text)
        self.assertIn("default-lane coordinator", text)
        self.assertIn("--mint-coordinator", text)
        self.assertIn("No worktree, pane, or dispatch was created", text)


class DeclaredGatewayNeverFallsBackTest(unittest.TestCase):
    """Design constraint 1: a declared topology never consults the coordinator."""

    def _probe_recorder(self):
        calls = []

        def probe():
            calls.append(True)
            return OK_PROBE

        return calls, probe

    def test_a_declared_and_verified_gateway_admits_without_the_probe(self) -> None:
        calls, probe = self._probe_recorder()

        verdict = decide_delegated_parent_authority(
            _bindings(GATEWAY_ENTRY),
            owner_row_active=lambda lane, scope: True,
            coordinator_parent_probe=probe,
        )

        self.assertTrue(verdict.ok)
        self.assertEqual(PARENT_KIND_PROJECT_GATEWAY, verdict.parent_kind)
        self.assertEqual([], calls)

    def test_a_declared_but_unverified_gateway_refuses_despite_a_live_coordinator(
        self,
    ) -> None:
        # The other direction of "no silent fallback": a workspace that DECLARED
        # the monorepo tier keeps the #15146 assert even when a live attested
        # coordinator exists right there.
        calls, probe = self._probe_recorder()

        verdict = decide_delegated_parent_authority(
            _bindings(GATEWAY_ENTRY),
            owner_row_active=lambda lane, scope: False,
            coordinator_parent_probe=probe,
        )

        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_GATEWAY_UNVERIFIED, verdict.reason)
        self.assertEqual(PARENT_KIND_PROJECT_GATEWAY, verdict.parent_kind)
        self.assertEqual([], calls)


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


_PROXY_SEND = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send"
)


@contextlib.contextmanager
def _attested_coordinator_reads():
    """Patch the probe's reads to a resolved, live, attested coordinator."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
        ProxyTarget,
    )

    with patch(
        f"{_PROXY_SEND}.resolve_default_lane_authority",
        return_value=("resolved", "coordinator", "proj-a", ""),
    ), patch(
        f"{_PROXY_SEND}.resolve_expected_provider", return_value="codex"
    ), patch(
        f"{_PROXY_SEND}.live_workspace_id", return_value="a" * 32
    ), patch(
        f"{_PROXY_SEND}.live_agent_rows", return_value=()
    ), patch(
        f"{_PROXY_SEND}.resolve_proxy_target",
        return_value=ProxyTarget(
            status="ok",
            assigned_name="mzb1-attested-coordinator",
            locator="%7",
            live=1,
            with_locator=1,
            attestation_state="attested",
        ),
    ):
        yield


class LiveGateCompositionTest(_TempRepo):
    """The gate composes the coordinator branch from the proxy rail's own reads."""

    def test_the_live_gate_admits_a_single_workspace_with_a_live_coordinator(
        self,
    ) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_refusal,
            delegated_parent_authority_verdict,
        )

        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])

        with _attested_coordinator_reads():
            verdict = delegated_parent_authority_verdict(
                repo, "delegated_coordinator"
            )
            refusal = delegated_parent_authority_refusal(
                repo, "delegated_coordinator"
            )

        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.ok)
        self.assertEqual(PARENT_KIND_DEFAULT_COORDINATOR, verdict.parent_kind)
        self.assertEqual(
            "mzb1-attested-coordinator", verdict.verified_coordinator
        )
        self.assertIsNone(refusal)

    def test_an_unattested_live_coordinator_refuses_through_the_gate(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
            ProxyTarget,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_verdict,
        )

        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])

        with _attested_coordinator_reads():
            with patch(
                f"{_PROXY_SEND}.resolve_proxy_target",
                return_value=ProxyTarget(
                    status="unattested",
                    live=1,
                    with_locator=1,
                    attestation_state="stale",
                    attestation_reason="generation mismatch",
                ),
            ):
                verdict = delegated_parent_authority_verdict(
                    repo, "delegated_coordinator"
                )

        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_UNATTESTED, verdict.reason)
        self.assertIn("stale", verdict.detail)

    def test_a_broken_probe_read_fails_closed(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegated_parent_authority_gate import (  # noqa: E501
            delegated_parent_authority_verdict,
        )

        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])

        with patch(
            f"{_PROXY_SEND}.resolve_default_lane_authority",
            side_effect=RuntimeError("registry unreadable"),
        ):
            verdict = delegated_parent_authority_verdict(
                repo, "delegated_coordinator"
            )

        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.ok)
        self.assertEqual(PARENT_COORDINATOR_BLOCKED, verdict.reason)


class BothEntryPointsAdmitIdenticallyTest(_TempRepo):
    """Plan and dry-run actuator surface the SAME admission and the taken branch."""

    def _plan_only(self, repo: Path):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_create,
        )

        args = argparse.Namespace(
            repo=str(repo),
            issue="15700",
            lane_label="issue_15700_probe",
            branch="issue_15700_probe",
            worktree="",
            journal="1",
            lane_kind="delegated_coordinator",
            json=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_sublane_create(args)
        return code, out.getvalue(), err.getvalue()

    def test_the_plan_only_surface_admits_and_names_the_branch(self) -> None:
        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])

        with _attested_coordinator_reads():
            code, out, err = self._plan_only(repo)

        # The admission passed: whatever the plan itself decides downstream, the
        # typed parent observation is on stderr and no refusal text appeared.
        self.assertIn(
            "parent_kind=" + PARENT_KIND_DEFAULT_COORDINATOR, err
        )
        self.assertNotIn("sublane create refused", out + err)
        # Zero side effect from the plan-only surface.
        self.assertFalse((repo / ".worktrees").exists())

    def test_the_dry_run_actuator_reaches_past_the_parent_admission(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            cmd_sublane_start,
        )

        repo = self._repo(bindings=[dict(COORDINATOR_ENTRY)])
        args = argparse.Namespace(
            repo=str(repo),
            issue="15700",
            lane_label="issue_15700_probe",
            branch="issue_15700_probe",
            worktree="",
            journal="1",
            lane_kind="delegated_coordinator",
            execute=False,
            dry_run=True,
            json=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with _attested_coordinator_reads():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cmd_sublane_start(args)

        text = err.getvalue()
        self.assertIn("parent_kind=" + PARENT_KIND_DEFAULT_COORDINATOR, text)
        self.assertNotIn("sublane create refused", out.getvalue() + text)
        self.assertFalse((repo / ".worktrees").exists())


class ExistingRailsSmokeTest(unittest.TestCase):
    """Design D4: the lane hangs from EXISTING rails — none of them moved.

    Minimal smoke for acceptance condition 3; the live end-to-end proof runs in
    the #15693 trial, not here.
    """

    def test_the_coordinator_forward_leg_reaches_delegated_coordinator(self) -> None:
        # L1 -> L2 in a single workspace: the coordinator's own forward leg.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_forward_route import (  # noqa: E501
            plan_forward_route,
        )

        route = plan_forward_route("coordinator", "")

        self.assertEqual("delegated_coordinator", route.to_role)

    def test_the_l2_to_l3_role_profile_chain_is_unchanged(self) -> None:
        # L2 -> L3: the fixed nested-handoff profile chain (f_140), reused as-is.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_coordinator_route_plan import (  # noqa: E501
            ROUTE_HOPS,
        )

        self.assertEqual(
            (
                ("parent_to_child", "delegated_coordinator"),
                ("child_to_gateway", "implementation_gateway"),
                ("gateway_to_worker", "implementation_worker"),
            ),
            tuple((hop, role) for hop, role in ROUTE_HOPS),
        )

    def test_the_upstream_callback_route_identity_is_the_coordinator_lane(
        self,
    ) -> None:
        # L2 -> L1: the callback outbox rail's default route names the workspace
        # coordinator lane — in a single workspace, that IS the L1 parent the
        # admission just verified live.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (  # noqa: E501
            DEFAULT_CALLBACK_ROUTE,
        )

        self.assertEqual("coordinator", DEFAULT_CALLBACK_ROUTE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
