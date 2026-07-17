"""Callback supervisor authoritative workspace partition — E2E (Redmine #13968).

Integration twin of the pure unit file (`tests/unit/.../test_supervisor_workspace_partition.py`):
wires the real :class:`WorkspaceCallbackSupervisor` to a real workflow-runtime store, callback
outbox, and supervisor lease store (temp-home SQLite) plus a mapping journal source, and drives a
whole ``run_once`` sweep. Pins the review corrections:

- **F1 (authoritative workspace uniqueness)** — when the same issue is visible in two workspaces'
  rosters, only the durable owning workspace supervises + delivers it; the other ingests and
  delivers nothing. An issue with no unique owner (ambiguous) is supervised nowhere (fail-closed).
- **F2 (historical dispatch-anchor fence)** — general callback candidates on a journal older than
  the current dispatch anchor are dropped (0-send); only the current-generation gate delivers.
- **Production authoritative wiring** — :func:`default_authoritative_map` over a REAL seeded
  lifecycle store resolves the unique authoritative workspace and omits an ambiguously-owned issue.
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
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleReader, LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer, LaneLifecycleKey
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    glance_snapshot_source as gss,
    sublane_herdr_projection as proj,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
    default_authoritative_map,
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
    issue: str
    lane_label: str
    lane_id: str
    workspace_id: str
    state: str = "active"


class _RecordingSender:
    def __init__(self, outcome: str = SEND_DELIVERED) -> None:
        self.calls: list = []
        self._outcome = outcome

    def __call__(self, row) -> str:
        self.calls.append(row)
        return self._outcome


def _review_payload(journals) -> dict:
    """A Redmine issue payload whose given journals each carry a review_request gate marker."""
    return {
        "issue": {"id": ISSUE},
        "journals": [
            {
                "id": str(j),
                "notes": (
                    "## Gate: review_request\n"
                    "[mozyo:workflow-event:gate=review_request:conclusion=pending]"
                ),
            }
            for j in journals
        ],
    }


class SupervisorWorkspacePartitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=self.store_path)
        self.outbox = CallbackOutbox(path=self.store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")
        self.sender = _RecordingSender()
        self.ws_a = SupervisedWorkspace(workspace_id=WS_A, canonical_path=str(self.dir / "repoA"))
        self.ws_b = SupervisedWorkspace(workspace_id=WS_B, canonical_path=str(self.dir / "repoB"))

    def _supervisor(self, *, source, authoritative_fn=None, candidate_fence_fn=None):
        return WorkspaceCallbackSupervisor(
            holder="superX",
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: [self.ws_a, self.ws_b],
            roster_fn=default_roster,
            redmine_source_fn=lambda ws: source,
            sender_fn=lambda ws: self.sender,
            clock=lambda: "2026-07-18T00:00:00+00:00",
            authoritative_fn=authoritative_fn,
            candidate_fence_fn=candidate_fence_fn,
        )

    def _patched_views(self, views):
        return patch.multiple(
            proj,
            repo_backend_is_herdr=lambda repo_root: True,
            herdr_sublane_views=lambda repo_root, **_kw: views,
        )

    # -- F1: authoritative workspace uniqueness ---------------------------

    def test_same_issue_in_two_workspaces_delivers_only_from_the_owner(self) -> None:
        # The reproduction shape: #13811 is live under BOTH workspaces' lanes, so the real
        # workspace-partitioned roster puts it in each workspace's roster. The durable authority
        # names WS_A the sole owner, so only WS_A supervises + delivers it.
        views = [
            _View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A),
            _View(ISSUE, "issue_13811_foreign", "issue_13811_foreign", WS_B),
        ]
        sup = self._supervisor(
            source=MappingRedmineJournalSource(payload=_review_payload(["79312"])),
            authoritative_fn=lambda: {ISSUE: WS_A},
        )
        with self._patched_views(views), patch.object(
            gss, "_lifecycle_disposition_by_unit", return_value={}
        ):
            report = sup.run_once()
        by_id = {w.workspace_id: w for w in report.workspaces}
        self.assertEqual(by_id[WS_A].supervised_issues, (ISSUE,))
        self.assertEqual(by_id[WS_A].delivered, 1)
        # WS_B saw the issue in its roster but is NOT the owner: dropped, zero-deliver.
        self.assertEqual(by_id[WS_B].supervised_issues, ())
        self.assertEqual(by_id[WS_B].non_authoritative_issues, (ISSUE,))
        self.assertEqual(by_id[WS_B].skipped_reason, SKIP_NO_ACTIVE_ISSUES)
        self.assertEqual(by_id[WS_B].delivered, 0)
        # Exactly one delivery across the whole sweep — no cross-workspace amplification.
        self.assertEqual(len(self.sender.calls), 1)

    def test_ambiguous_owner_is_supervised_nowhere(self) -> None:
        # No unique authoritative workspace (the issue is omitted from the map): fail-closed —
        # neither workspace delivers, so an unresolvable ownership never double-sends.
        views = [
            _View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A),
            _View(ISSUE, "issue_13811_foreign", "issue_13811_foreign", WS_B),
        ]
        sup = self._supervisor(
            source=MappingRedmineJournalSource(payload=_review_payload(["79312"])),
            authoritative_fn=lambda: {},  # ambiguous / absent -> omitted
        )
        with self._patched_views(views), patch.object(
            gss, "_lifecycle_disposition_by_unit", return_value={}
        ):
            report = sup.run_once()
        by_id = {w.workspace_id: w for w in report.workspaces}
        for wsid in (WS_A, WS_B):
            self.assertEqual(by_id[wsid].supervised_issues, ())
            self.assertEqual(by_id[wsid].non_authoritative_issues, (ISSUE,))
        self.assertEqual(self.sender.calls, [])

    # -- F2: historical dispatch-anchor fence -----------------------------

    def test_historical_gate_older_than_anchor_is_fenced(self) -> None:
        # The issue journal carries a historical review gate (j100) AND a current one (j300).
        # The dispatch anchor is j206, so only the current gate is a candidate; the historical
        # one is fenced (0-send), and exactly one delivery happens.
        views = [_View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A)]
        sup = self._supervisor(
            source=MappingRedmineJournalSource(payload=_review_payload(["100", "300"])),
            authoritative_fn=lambda: {ISSUE: WS_A},
            candidate_fence_fn=lambda wsid, issue, source: "206",
        )
        with self._patched_views(views), patch.object(
            gss, "_lifecycle_disposition_by_unit", return_value={}
        ):
            report = sup.run_once()
        ws_a = {w.workspace_id: w for w in report.workspaces}[WS_A]
        issue_outcome = ws_a.issues[0]
        self.assertEqual(issue_outcome.historical_fenced, 1)  # j100 dropped
        self.assertEqual(issue_outcome.delivered, 1)  # only j300 delivered
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sender.calls[0].journal, "300")

    def test_unresolvable_anchor_fences_all_general_candidates(self) -> None:
        # A ``None`` anchor (owning lane / structured IR could not be pinned) fails closed: every
        # general candidate is dropped, zero delivery.
        views = [_View(ISSUE, "issue_13811_a", "issue_13811_a", WS_A)]
        sup = self._supervisor(
            source=MappingRedmineJournalSource(payload=_review_payload(["300"])),
            authoritative_fn=lambda: {ISSUE: WS_A},
            candidate_fence_fn=lambda wsid, issue, source: None,
        )
        with self._patched_views(views), patch.object(
            gss, "_lifecycle_disposition_by_unit", return_value={}
        ):
            report = sup.run_once()
        ws_a = {w.workspace_id: w for w in report.workspaces}[WS_A]
        self.assertEqual(ws_a.issues[0].historical_fenced, 1)
        self.assertEqual(ws_a.issues[0].delivered, 0)
        self.assertEqual(self.sender.calls, [])


class DefaultAuthoritativeMapTest(unittest.TestCase):
    """Production wiring (real lifecycle store): the durable authority selects the owner."""

    def _seed(self, home, ws, lane, issue):
        store = LaneLifecycleStore(home=home)
        store.declare_active(
            LaneLifecycleKey(ws, lane),
            decision=DecisionPointer(source="redmine", issue_id=issue, journal_id="79312"),
            issue_id=issue,
        )

    def test_unique_active_owner_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._seed(home, WS_A, "issue_13811_a", ISSUE)
            m = default_authoritative_map(LaneLifecycleReader(home=home))
        self.assertEqual(m.get(ISSUE), WS_A)

    def test_issue_actively_owned_in_two_workspaces_is_omitted(self) -> None:
        # The workspace-scoped owner index allows each workspace one active owner for the same
        # issue id; across workspaces that is ambiguous, so the issue maps to no authoritative
        # workspace (fail-closed) — the case F1 must not double-deliver.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._seed(home, WS_A, "issue_13811_a", ISSUE)
            self._seed(home, WS_B, "issue_13811_b", ISSUE)
            m = default_authoritative_map(LaneLifecycleReader(home=home))
        self.assertNotIn(ISSUE, m)


if __name__ == "__main__":
    unittest.main()
