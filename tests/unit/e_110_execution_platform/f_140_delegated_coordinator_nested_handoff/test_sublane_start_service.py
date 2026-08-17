"""The typed sublane-start service: one body, every gate, typed refusals (#15152).

``run_sublane_start`` is the shared actuation body behind the CLI's
``cmd_sublane_start`` and the MCP ``sublane_start`` tool. These specs pin what
makes it safe to have two entries: the admission order is the CLI's exact
historical order (work-unit config -> #15146 parent authority -> provider
launchability), every refusal is typed and decided before the use case (and so
before any side effect), and the use case receives the same request fields the
CLI adapter used to build.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_start_service as svc  # noqa: E402,E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E402,E501
    PARENT_GATEWAY_UNDECLARED,
)


class _TempRepo(unittest.TestCase):
    def _repo(self, bindings=None, config_text=None) -> Path:
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
        if config_text is not None:
            (repo / ".mozyo-bridge" / "config.yaml").write_text(config_text)
        return repo

    def _command(self, repo: Path, **overrides) -> svc.SublaneStartCommand:
        fields = dict(
            repo_root=repo,
            issue="15152",
            lane_label="issue_15152_probe",
            branch="issue_15152_probe",
        )
        fields.update(overrides)
        return svc.SublaneStartCommand(**fields)


class AdmissionRefusalTests(_TempRepo):
    def test_a_broken_repo_local_config_refuses_typed_first(self) -> None:
        repo = self._repo(config_text="version: 1\nwork_unit: {granularity: bogus}\n")

        with patch.object(svc, "provider_preflight_refusal") as preflight:
            result = svc.run_sublane_start(self._command(repo))

        self.assertTrue(result.refused)
        self.assertEqual(svc.REFUSAL_INVALID_REPO_CONFIG, result.refusal.reason)
        self.assertEqual(1, result.exit_code)
        # The config gate is FIRST: nothing after it ran.
        self.assertEqual(0, preflight.call_count)

    def test_parent_authority_refuses_before_the_provider_preflight(self) -> None:
        # The #15146 admission, through the typed service: a delegated_coordinator
        # with a coordinator-only declaration refuses with the verdict's own
        # closed reason token, and the provider preflight is never consulted.
        repo = self._repo(bindings=[{"role": "coordinator", "project_scope": "p"}])

        with patch.object(svc, "provider_preflight_refusal") as preflight:
            result = svc.run_sublane_start(
                self._command(repo, lane_kind="delegated_coordinator")
            )

        self.assertTrue(result.refused)
        self.assertEqual(PARENT_GATEWAY_UNDECLARED, result.refusal.reason)
        self.assertEqual(0, preflight.call_count)

    def test_a_provider_refusal_stops_before_the_use_case(self) -> None:
        repo = self._repo()
        blocked = svc.SublaneStartRefusal(
            reason=svc.REFUSAL_PROVIDER_NOT_LAUNCHABLE, message="blocked"
        )

        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        with patch.object(svc, "provider_preflight_refusal", return_value=blocked):
            with patch.object(act, "SublaneActuateUseCase") as use_case:
                result = svc.run_sublane_start(self._command(repo))

        self.assertTrue(result.refused)
        self.assertEqual(svc.REFUSAL_PROVIDER_NOT_LAUNCHABLE, result.refusal.reason)
        # Refused strictly before the actuation body: no use case, no ops.
        self.assertEqual(0, use_case.call_count)

    def test_the_provider_snapshot_reaches_the_preflight(self) -> None:
        repo = self._repo()
        snapshot = object()
        seen = {}

        def _spy(repo_root, snapshot=None):
            seen["snapshot"] = snapshot
            return svc.SublaneStartRefusal(reason="provider_unresolved", message="x")

        with patch.object(svc, "provider_preflight_refusal", _spy):
            svc.run_sublane_start(self._command(repo, provider_snapshot=snapshot))

        self.assertIs(snapshot, seen["snapshot"])


class _CapturingUseCase:
    """Stands in for SublaneActuateUseCase; records the request and run kwargs."""

    captured: dict = {}

    def __init__(self, ops, **kwargs):
        type(self).captured["init"] = kwargs

    def run(self, request, **kwargs):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
            SublaneActuationOutcome,
        )

        type(self).captured["request"] = request
        type(self).captured["run"] = kwargs
        return SublaneActuationOutcome(
            status="blocked",
            execute=bool(kwargs.get("execute")),
            reason="missing_identity",
            issue=request.issue,
            lane_label=request.lane_label,
            blocked_reasons=("missing_identity",),
        )


class CompletedPathTests(_TempRepo):
    def _run(self, repo: Path, **overrides):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

        _CapturingUseCase.captured = {}
        with patch.object(svc, "provider_preflight_refusal", return_value=None):
            with patch.object(act, "_resolve_sublane_ops", return_value=object()):
                with patch.object(act, "SublaneActuateUseCase", _CapturingUseCase):
                    return svc.run_sublane_start(self._command(repo, **overrides))

    def test_the_request_carries_the_cli_equivalent_fields(self) -> None:
        # The #14224 lesson, restated for the service: the request the use case
        # receives carries the same admission inputs regardless of the entry.
        repo = self._repo()

        self._run(
            repo,
            work_unit="leaf_issue",
            leaf_standalone=True,
            base_ref="origin/main",
            lane_kind="implementation",
            journal="42",
        )

        request = _CapturingUseCase.captured["request"]
        self.assertEqual("leaf_issue", request.work_unit)
        self.assertTrue(request.leaf_standalone)
        self.assertEqual("origin/main", request.base_ref)
        self.assertEqual("implementation", request.lane_kind)
        self.assertEqual("42", request.journal)

    def test_execute_and_dispatch_flags_reach_the_run(self) -> None:
        repo = self._repo()

        self._run(repo, execute=True, dispatch=False, target_repo="auto")

        run = _CapturingUseCase.captured["run"]
        self.assertTrue(run["execute"])
        self.assertFalse(run["dispatch"])

    def test_a_blocked_outcome_maps_to_exit_code_one(self) -> None:
        repo = self._repo()

        result = self._run(repo)

        self.assertFalse(result.refused)
        self.assertEqual(svc.STATUS_COMPLETED, result.status)
        self.assertEqual(1, result.exit_code)
        self.assertTrue(result.outcome.is_blocked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
