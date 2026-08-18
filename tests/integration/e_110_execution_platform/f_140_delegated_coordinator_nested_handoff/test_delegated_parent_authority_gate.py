"""Live composition of the coordinator-parent admission gate (Redmine #15700).

The application gate (``delegated_parent_authority_gate``) wired to its REAL
collaborators — the repo-local role-binding declaration read from a real
temp-repo filesystem, and the coordinator proxy rail's resolvers faked only at
their own import seam — verifying that the single-workspace branch's verdict
composes end to end: admit on a live attested coordinator, typed refusal on an
unattested one, and fail-closed on a broken read.

Integration per the placement policy (review j#107924 finding_testplacement):
multiple real collaborators (gate + bindings loader + real temp filesystem) are
wired together, hermetically. The pure branch decision stays module-isolated in
``tests/unit/.../test_delegated_parent_authority_coordinator_branch``; the
operator-view command acceptance lives in
``tests/scenarios/test_single_workspace_delegated_coordinator_acceptance``.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegated_parent_authority import (  # noqa: E402,E501
    PARENT_COORDINATOR_BLOCKED,
    PARENT_COORDINATOR_UNATTESTED,
    PARENT_KIND_DEFAULT_COORDINATOR,
)

_PROXY_SEND = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send"
)

COORDINATOR_ENTRY = {"role": "coordinator", "project_scope": "proj-a"}


@contextlib.contextmanager
def _attested_coordinator_reads():
    """Patch the gate probe's proxy-rail reads to a resolved, live, attested coordinator."""
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


class LiveGateCompositionTest(unittest.TestCase):
    """The gate composes the coordinator branch from the proxy rail's own reads."""

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
