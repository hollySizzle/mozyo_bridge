"""Callback supervisor authoritative workspace partition (Redmine #13968).

The callback supervisor fanned out over the whole workspace registry and, per registry
workspace, resolved its active-lane roster from ``enumerate_active_lanes`` — which returns the
**host-global** live lane inventory (the herdr ``agent list`` is host-wide by the #13331
contract). So every registry workspace received the SAME roster and re-ingested + re-delivered
every active issue into its own outbox partition: one active issue (e.g. #13811 / #13933 /
#13948) was projected into every foreign / stale registry workspace, amplifying pending /
dead-letter on each run and delivering the callback from workspaces that do not own the lane.

The fix partitions the roster to the lane's durable ``workspace_id`` — the registry / anchor
identity ``herdr_workspace_segment`` stamps into every managed slot name — matched against the
supervised workspace's own registry id. These tests prove:

1. the pure enumeration (:func:`enumerate_active_lanes_for_workspace`) returns ONLY the lanes the
   registry attributes to the requested workspace; a foreign id gets an empty roster; the
   host-global :func:`enumerate_active_lanes` is unchanged (non-regression);
2. end-to-end, with a high-fidelity two-workspace host inventory where the same issue appears
   only under its authoritative workspace, exactly ONE workspace supervises + delivers it, and
   the foreign registry workspace ingests nothing and delivers nothing (acceptance 1 / 5).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    glance_snapshot_source as gss,
    sublane_herdr_projection as proj,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
    enumerate_active_lanes,
    enumerate_active_lanes_for_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    default_roster,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MappingRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_NO_ACTIVE_ISSUES,
)

WS_A = "wsAuthoritative"
WS_B = "wsForeign"
ISSUE = "13811"


@dataclass
class _View:
    """The minimal ``SublaneLaneView`` shape the roster fold reads (issue + workspace_id)."""

    issue: str
    lane_label: str
    lane_id: str
    workspace_id: str
    state: str = "active"


def _review_request_payload(issue: str = ISSUE, journal: str = "79312") -> dict:
    return {
        "issue": {"id": issue},
        "journals": [
            {
                "id": journal,
                "notes": (
                    "## Gate: review_request\n"
                    "[mozyo:workflow-event:gate=review_request:conclusion=pending]"
                ),
            }
        ],
    }


class _RecordingSender:
    def __init__(self, outcome: str = SEND_DELIVERED) -> None:
        self.calls: list = []
        self._outcome = outcome

    def __call__(self, row) -> str:
        self.calls.append(row)
        return self._outcome


class EnumerateActiveLanesPartitionTest(unittest.TestCase):
    """Acceptance 5 (pure): the host-global inventory partitions by durable workspace_id."""

    def _host_global_views(self):
        # The SAME issue id is live under TWO distinct workspaces (its authoritative one and a
        # foreign lane that also carries it), exactly the reproduction where an active issue was
        # visible in more than one registry workspace's roster.
        return [
            _View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A),
            _View("13933", "issue_13933_a", "issue_13933_a", WS_A),
            _View(ISSUE, "issue_13811_foreign", "issue_13811_foreign", WS_B),
        ]

    def _patched(self):
        return patch.multiple(
            proj,
            repo_backend_is_herdr=lambda repo_root: True,
            herdr_sublane_views=lambda repo_root, **_kw: self._host_global_views(),
        )

    def test_partition_returns_only_the_requested_workspace_lanes(self) -> None:
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster_a, err_a = enumerate_active_lanes_for_workspace(Path("."), workspace_id=WS_A)
            roster_b, err_b = enumerate_active_lanes_for_workspace(Path("."), workspace_id=WS_B)
            roster_foreign, err_f = enumerate_active_lanes_for_workspace(
                Path("."), workspace_id="wsNobody"
            )
        self.assertIsNone(err_a)
        self.assertIsNone(err_b)
        self.assertIsNone(err_f)
        # WS_A owns the #13811 + #13933 lanes; WS_B owns only the foreign #13811 lane.
        self.assertEqual(roster_a, ((ISSUE, "issue_13811_a"), ("13933", "issue_13933_a")))
        self.assertEqual(roster_b, ((ISSUE, "issue_13811_foreign"),))
        # A registry workspace that owns no live lane supervises nothing: zero roster.
        self.assertEqual(roster_foreign, ())

    def test_blank_workspace_id_matches_nothing(self) -> None:
        # Fail-closed: an unidentifiable workspace never inherits the whole host.
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster, err = enumerate_active_lanes_for_workspace(Path("."), workspace_id="")
        self.assertIsNone(err)
        self.assertEqual(roster, ())

    def test_host_global_enumeration_unchanged(self) -> None:
        # Non-regression: the coordinator glance still sees every live lane on the host.
        with self._patched(), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            roster, err = enumerate_active_lanes(Path("."))
        self.assertIsNone(err)
        self.assertEqual(
            set(roster),
            {(ISSUE, "issue_13811_a"), ("13933", "issue_13933_a"), (ISSUE, "issue_13811_foreign")},
        )


class SupervisorWorkspacePartitionTest(unittest.TestCase):
    """Acceptance 1 / 5 (end-to-end): exactly one workspace delivers; foreign one delivers zero."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.lease_path = self.dir / "supervisor-lease.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.lease_path)
        self.sender = _RecordingSender()
        self.source = MappingRedmineJournalSource(payload=_review_request_payload())
        self.ws_a = SupervisedWorkspace(workspace_id=WS_A, canonical_path=str(self.dir / "repoA"))
        self.ws_b = SupervisedWorkspace(workspace_id=WS_B, canonical_path=str(self.dir / "repoB"))

    def _host_global_views(self):
        # #13811's lane is owned ONLY by WS_A. WS_B is a stale registry row that owns no lane.
        return [_View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A)]

    def test_only_authoritative_workspace_delivers_the_shared_issue(self) -> None:
        supervisor = WorkspaceCallbackSupervisor(
            holder="superX",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws_a, self.ws_b],
            # The REAL partitioned roster resolver over a fixed host-global inventory.
            roster_fn=default_roster,
            redmine_source_fn=lambda ws: self.source,
            sender_fn=lambda ws: self.sender,
            clock=lambda: "2026-07-18T00:00:00+00:00",
        )
        with patch.multiple(
            proj,
            repo_backend_is_herdr=lambda repo_root: True,
            herdr_sublane_views=lambda repo_root, **_kw: self._host_global_views(),
        ), patch.object(gss, "_lifecycle_disposition_by_unit", return_value={}):
            report = supervisor.run_once()

        by_id = {w.workspace_id: w for w in report.workspaces}
        # WS_A owns #13811: it supervises + delivers exactly once.
        self.assertEqual(by_id[WS_A].supervised_issues, (ISSUE,))
        self.assertEqual(by_id[WS_A].delivered, 1)
        # WS_B owns no live lane: zero-ingest / zero-deliver (foreign registry).
        self.assertEqual(by_id[WS_B].supervised_issues, ())
        self.assertEqual(by_id[WS_B].skipped_reason, SKIP_NO_ACTIVE_ISSUES)
        self.assertEqual(by_id[WS_B].delivered, 0)
        # Exactly one delivery across the whole sweep — no cross-workspace amplification.
        self.assertEqual(len(self.sender.calls), 1)


if __name__ == "__main__":
    unittest.main()
