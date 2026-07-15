"""Pure workspace-supervision domain tests (Redmine #13683 Phase A).

Pins the pure decisions the composition root composes: which issues a pass supervises under each
wake mode (and that a wake for a non-active issue is ignored, not trusted), the redaction-safe
report roll-up, and the secret-free service definition + Phase-A host-mutation gate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    PHASE_A_SERVICE_MUTATION_REASON,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_WAKE,
    IssueSupervisionOutcome,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
    build_service_definition,
    select_supervised_issues,
)


class SelectSupervisedIssuesTest(unittest.TestCase):
    def test_bounded_reconciliation_supervises_whole_roster(self) -> None:
        sel = select_supervised_issues(
            ["13683", "13684", "13683"], mode=SUPERVISION_BOUNDED_RECONCILIATION
        )
        self.assertEqual(sel.supervised, ("13683", "13684"))  # de-duplicated, order-preserving
        self.assertEqual(sel.ignored_wake, ())

    def test_local_wake_supervises_only_wake_named_roster_issues(self) -> None:
        sel = select_supervised_issues(
            ["13683", "13684", "13999"],
            mode=SUPERVISION_LOCAL_WAKE,
            wake_issues=["13684"],
        )
        self.assertEqual(sel.supervised, ("13684",))
        self.assertEqual(sel.ignored_wake, ())

    def test_local_wake_for_non_active_issue_is_ignored_not_trusted(self) -> None:
        # The roster is the authority on what is active; a wake naming a retired / foreign issue
        # is surfaced as ignored, never supervised.
        sel = select_supervised_issues(
            ["13683"], mode=SUPERVISION_LOCAL_WAKE, wake_issues=["99999", "13683"]
        )
        self.assertEqual(sel.supervised, ("13683",))
        self.assertEqual(sel.ignored_wake, ("99999",))

    def test_unknown_mode_falls_back_to_bounded_reconciliation(self) -> None:
        sel = select_supervised_issues(["13683"], mode="nonsense", wake_issues=["x"])
        self.assertEqual(sel.supervised, ("13683",))


class ReportRollupTest(unittest.TestCase):
    def test_rollup_counts_supervised_vs_skipped_and_totals(self) -> None:
        ws_ok = WorkspaceSupervisionOutcome(
            workspace_id="wsA",
            lease_acquired=True,
            lease_reason="granted_fresh",
            supervised_issues=("13683",),
            issues=(IssueSupervisionOutcome(issue="13683", events_supplied=2, delivered=1),),
        )
        ws_skipped = WorkspaceSupervisionOutcome(
            workspace_id="wsB", lease_acquired=False, lease_reason="refused_held_by_other",
            skipped_reason="lease_held_by_other",
        )
        report = SupervisorReport(
            mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="superX",
            workspaces=(ws_ok, ws_skipped),
        )
        self.assertEqual(report.workspaces_supervised, 1)
        self.assertEqual(report.workspaces_skipped, 1)
        self.assertEqual(report.events_supplied, 2)
        self.assertEqual(report.delivered, 1)
        payload = report.as_payload()
        self.assertEqual(payload["workspaces_total"], 2)
        self.assertEqual(payload["events_supplied"], 2)
        # Redaction-safe: no path / credential leaks in the structured payload.
        self.assertNotIn("canonical_path", str(payload))


class ServiceDefinitionTest(unittest.TestCase):
    def test_definition_is_secret_free_and_run_once_shaped(self) -> None:
        d = build_service_definition()
        self.assertEqual(d.command[-1], "--run-once")
        self.assertEqual(d.reconciliation_interval_seconds, DEFAULT_RECONCILIATION_INTERVAL_SECONDS)
        blob = str(d.as_payload()).lower()
        for secret_word in ("api_key", "apikey", "password", "token", "secret"):
            self.assertNotIn(secret_word, blob)

    def test_interval_is_clamped_to_at_least_one(self) -> None:
        self.assertEqual(build_service_definition(reconciliation_interval_seconds=0).reconciliation_interval_seconds, 1)

    def test_phase_a_mutation_reason_is_stable(self) -> None:
        self.assertEqual(PHASE_A_SERVICE_MUTATION_REASON, "phase_a_no_host_service_mutation")


if __name__ == "__main__":
    unittest.main()
