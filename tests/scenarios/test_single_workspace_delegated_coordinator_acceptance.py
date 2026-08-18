"""Single-workspace delegated_coordinator lane: operator-view acceptance (Redmine #15700).

Acceptance conditions 1 and 3 of #15700, driven end to end through the REAL surfaces
(review j#107906 finding_acceptancetest: an entry-point drive that never asserts the
return code passes even when the command exits 1 on a malformed request, and a
constant comparison is not a callback smoke):

1. the operator's full ``sublane create --lane-kind delegated_coordinator`` argv —
   real parser, real command handler — SUCCEEDS (exit 0, a planned/ready outcome)
   in a workspace with no ``project_gateway`` binding, with the coordinator-parent
   reads faked at their import seam to a live attested coordinator, and REFUSES
   (exit 1, typed) with the same full argv when no live coordinator exists;
2. the L2->L1 / L2->L3 legs ride the EXISTING rails: the real callback discovery
   planner routes a durable gate marker to the ``coordinator`` route, the real
   ingest-side resolution maps that route to the default lane + binding-resolved
   coordinator provider, and the real forward/route-plan tables name the
   coordinator->delegated_coordinator and L2->L3 profile chain.

Cross-cutting (CLI -> execution platform admission -> callback rail), so it lives
in ``tests/scenarios/`` per the placement policy. The branch decision itself is
unit-pinned in ``tests/unit/.../test_delegated_parent_authority_coordinator_branch``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_PROXY_SEND = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send"
)


@contextlib.contextmanager
def _attested_coordinator_reads():
    """The coordinator-parent reads, faked at their import seam: live + attested."""
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


class SingleWorkspaceCreateAcceptanceTest(unittest.TestCase):
    """The full operator argv succeeds — and fails closed — through both surfaces."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": "a" * 32,
                    "canonical_session": "mozyo-scenario",
                    "project_name": "scenario",
                    "created_at": "2026-08-16T00:00:00+00:00",
                    "updated_at": "2026-08-16T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        # A single workspace: a coordinator binding and NO project_gateway anywhere.
        (self.repo / ".mozyo-bridge" / "workflow-role-bindings.json").write_text(
            json.dumps(
                {
                    "schema": "mozyo.workflow-role-bindings",
                    "version": 1,
                    "bindings": [
                        {"role": "coordinator", "project_scope": "proj-a"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        # The lane worktree path the plan will offer to create — it must NOT exist
        # yet, and it must still not exist afterwards (plan/dry-run actuate nothing).
        self.worktree = self.root / "lane-worktree"
        self.home = self.root / "home"
        self.home.mkdir()

    def _run(self, extra):
        """Parse the FULL operator argv with the real parser, run the real handler."""
        import mozyo_bridge.application.cli as cli
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        args = cli.build_parser().parse_args(
            [
                "sublane",
                "create",
                "--issue",
                "15700",
                "--lane-label",
                "issue_15700_accept",
                "--branch",
                "issue_15700_accept",
                "--worktree",
                str(self.worktree),
                "--journal",
                "1",
                "--repo",
                str(self.repo),
                "--lane-kind",
                "delegated_coordinator",
                *extra,
            ]
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = act.cmd_sublane_start(args)
        return code, out.getvalue(), err.getvalue()

    def _assert_no_side_effect(self) -> None:
        self.assertFalse(self.worktree.exists())
        self.assertFalse((self.repo / ".worktrees").exists())

    def test_the_plan_only_surface_succeeds_with_the_full_request(self) -> None:
        with _attested_coordinator_reads():
            code, out, err = self._run([])

        self.assertEqual(0, code)
        self.assertIn("sublane create: planned", out)
        # The taken branch is observable, and it is the single-workspace one.
        self.assertIn("parent_kind=default_lane_coordinator", err)
        self.assertNotIn("refused", out + err)
        # The planned dispatch rides the existing rail with the coordinator as the
        # upstream callback route identity — no new raw path.
        self.assertIn("upstream_coordinator=coordinator", out)
        self._assert_no_side_effect()

    def test_the_dry_run_actuator_reports_ready_with_the_full_request(self) -> None:
        with _attested_coordinator_reads():
            code, out, err = self._run(["--dry-run"])

        self.assertEqual(0, code)
        self.assertIn("sublane start (dry-run): ready", out)
        self.assertIn("parent_kind=default_lane_coordinator", err)
        self.assertNotIn("refused", out + err)
        self._assert_no_side_effect()

    def test_the_same_full_request_fails_closed_without_a_live_coordinator(
        self,
    ) -> None:
        # No patched reads: this hermetic environment has no live attested
        # coordinator, so the SAME complete argv must refuse typed — proving the
        # success above is earned by the parent verification, not by the fixture.
        code, out, err = self._run([])

        self.assertEqual(1, code)
        self.assertIn("sublane create refused", err)
        self.assertIn("parent_coordinator_", err)
        self._assert_no_side_effect()


class CallbackRailSmokeTest(unittest.TestCase):
    """The L2 legs ride the existing rails, driven through their real planners."""

    def test_a_durable_gate_marker_routes_to_the_coordinator_callback(self) -> None:
        # L2 -> L1: the REAL discovery planner over a REAL rendered gate marker —
        # a blocked gate on the lane anchor issue becomes a callback candidate
        # targeting the coordinator route (not a constant comparison).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (  # noqa: E501
            DEFAULT_CALLBACK_ROUTE,
            discover_candidates,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            MappingRedmineJournalSource,
            render_gate_note,
        )

        note = render_gate_note(
            "blocked", body="## Progress Log — L2 blocked, calling back to L1"
        )
        source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": 15700},
                "journals": [{"id": 424242, "notes": note}],
            }
        )

        candidates = discover_candidates(source, "15700")

        self.assertEqual(1, len(candidates))
        self.assertEqual(DEFAULT_CALLBACK_ROUTE, candidates[0].callback_route)
        self.assertEqual("15700", candidates[0].issue)
        self.assertEqual("424242", candidates[0].journal)
        self.assertEqual("blocked", candidates[0].notification_kind)

    def test_the_coordinator_route_resolves_to_the_default_lane_target(self) -> None:
        # L2 -> L1, delivery side: the REAL ingest-time resolution maps the
        # coordinator route to (default lane, binding-resolved coordinator
        # provider) — the exact target tuple the outbox row records. A
        # non-coordinator route resolves to nothing (fail-closed).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (  # noqa: E501
            DEFAULT_CALLBACK_ROUTE,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (  # noqa: E501
            coordinator_target_tuple,
        )

        class _Binding:
            def provider_for(self, role):
                return "codex" if role == "coordinator" else ""

        self.assertEqual(
            ("default", "codex"),
            coordinator_target_tuple(_Binding(), DEFAULT_CALLBACK_ROUTE),
        )
        self.assertEqual(
            ("", ""), coordinator_target_tuple(_Binding(), "lane_gateway:x")
        )

    def test_the_coordinator_forward_leg_reaches_delegated_coordinator(self) -> None:
        # L1 -> L2: the coordinator's own forward leg in the real route table.
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
