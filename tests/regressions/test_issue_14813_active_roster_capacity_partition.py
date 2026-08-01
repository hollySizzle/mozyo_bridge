"""Redmine #14813 — inventory residue must not be counted as active lane capacity.

Observed on ``origin/main`` before the fix (#14813 description, j#96022): ``workflow glance
--active-lanes --json`` folded **32 rows**, of which 25 were closed issues and 19 resolved to
``execution_surface=unknown``. All 19 were ``state=detached`` with ``gateway_pane=null``,
``worker_pane=null``, ``panes=[]`` and **no lifecycle row** — legacy inventory residue, not
running lanes. ``_fold_active_roster`` kept every lifecycle-unknown view ("over-counting is
conservative"), so the residue entered the active roster, degraded the projection, and stopped
every admission on ``stop_unverified_surface``. Capacity was 0 while nothing was actually full.

The fix partitions on **residency evidence**, not on the issue's Redmine state:

* no lifecycle row **and** no slots  -> residue (diagnostic, not capacity);
* no lifecycle row **but** slots     -> unverified LIVE claim, still counted, still fails closed;
* lifecycle row                      -> decided by its disposition, exactly as before.

Fixed by commit ``69049e2c``'s successor on ``issue_14813_active_roster_projection_mainunit_r1``.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    glance_snapshot_source as source,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (  # noqa: E501
    SURFACE_DETACHED_WORKTREE,
)

WORKSPACE = "ws-current"
FOREIGN_WORKSPACE = "ws-other"


@dataclass(frozen=True)
class _View:
    """The subset of ``SublaneLaneView`` the roster fold reads."""

    workspace_id: str
    lane_id: str
    issue: str
    lane_label: str = ""
    gateway_pane: Optional[str] = None
    worker_pane: Optional[str] = None
    panes: Tuple[str, ...] = field(default=())
    #: The read model's published liveness label. Residue is `detached`; a live view keeps the
    #: default, matching the roster fixtures that carry `state` without pane fields.
    state: str = "active"


def _residue(workspace_id: str, lane_id: str, issue: str) -> _View:
    """A detached row exactly as ``sublane list`` reported all 19: no pair, no panes."""
    return _View(workspace_id=workspace_id, lane_id=lane_id, issue=issue, state="detached")


def _live(workspace_id: str, lane_id: str, issue: str) -> _View:
    return _View(
        workspace_id=workspace_id,
        lane_id=lane_id,
        issue=issue,
        gateway_pane="w1:p2",
        worker_pane="w1:p3",
        panes=("w1:p2", "w1:p3"),
    )


class ActiveRosterCapacityPartitionTest(unittest.TestCase):
    """``_fold_active_roster`` — what counts as capacity and what is only a diagnostic."""

    def _fold(self, views, *, dispositions=None, workspace_id=WORKSPACE):
        original = source._lifecycle_disposition_by_unit
        source._lifecycle_disposition_by_unit = lambda: dict(dispositions or {})
        try:
            return source._fold_active_roster(views, workspace_id=workspace_id)
        finally:
            source._lifecycle_disposition_by_unit = original

    def test_lifecycle_unknown_row_without_slots_is_not_capacity(self) -> None:
        roster, residue = self._fold([_residue(WORKSPACE, "lane-a", "14001")])
        self.assertEqual(
            roster,
            (),
            "a detached view with no lifecycle row and no panes cannot occupy a managed "
            "sublane slot; counting it as capacity is what produced 19 unknown-surface rows",
        )
        self.assertEqual(
            residue,
            (("14001", "lane-a", SURFACE_DETACHED_WORKTREE),),
            "residue must stay visible as a typed diagnostic, not silently disappear",
        )

    def test_lifecycle_unknown_row_with_slots_still_counts_and_fails_closed(self) -> None:
        # The conservative direction that must NOT be lost: an owner-unbound lane that is
        # actually running is an unverified live claim, not residue.
        roster, residue = self._fold([_live(WORKSPACE, "lane-b", "14002")])
        self.assertEqual(roster, (("14002", "lane-b"),))
        self.assertEqual(residue, ())

    def test_one_pane_alone_is_enough_to_count(self) -> None:
        # Residency is evidence-of-any-slot, not evidence-of-a-complete-pair: a half-released
        # lane still holds a process.
        half = _View(
            workspace_id=WORKSPACE, lane_id="lane-c", issue="14003", gateway_pane="w1:p2"
        )
        roster, residue = self._fold([half])
        self.assertEqual(roster, (("14003", "lane-c"),))
        self.assertEqual(residue, ())

    def test_closed_issue_whose_pair_still_runs_keeps_consuming_capacity(self) -> None:
        # The partition is on residency, never on the issue's Redmine state. 25 of the 32
        # observed rows were closed issues; a closed issue whose pair is alive still occupies
        # a slot, so "closed" must not become a capacity exemption.
        roster, _ = self._fold([_live(WORKSPACE, "lane-d", "14004")])
        self.assertEqual(
            roster,
            (("14004", "lane-d"),),
            "issue state is not a residency signal; only the running slots are",
        )

    def test_non_active_disposition_still_wins_over_residency(self) -> None:
        roster, residue = self._fold(
            [_live(WORKSPACE, "lane-e", "14005")],
            dispositions={(WORKSPACE, "lane-e"): "hibernated"},
        )
        self.assertEqual(roster, (), "#13681 disposition exclusion must be unchanged")
        self.assertEqual(
            residue,
            (),
            "a lifecycle-known lane belongs to the lifecycle diagnostic, not the residue list",
        )

    def test_foreign_workspace_row_is_neither_capacity_nor_residue(self) -> None:
        roster, residue = self._fold([_residue(FOREIGN_WORKSPACE, "lane-f", "14006")])
        self.assertEqual(roster, ())
        self.assertEqual(
            residue,
            (),
            "another workspace's row is not this workspace's debt; #13968 partitions it out "
            "before the residency test",
        )

    def test_mixed_population_leaves_only_the_running_lanes_as_capacity(self) -> None:
        # The shape of the observed evidence, minimised: residue dominates the row count while
        # exactly one lane is actually resident.
        views = [
            _residue(WORKSPACE, f"stale-{i}", f"140{i:02d}") for i in range(10, 19)
        ] + [
            _residue(FOREIGN_WORKSPACE, "foreign", "14099"),
            _live(WORKSPACE, "resident", "14100"),
        ]
        roster, residue = self._fold(views)
        self.assertEqual(
            roster,
            (("14100", "resident"),),
            "capacity is the resident lanes only — 11 rows in, 1 slot occupied",
        )
        self.assertEqual(len(residue), 9, "every current-workspace residue row stays reported")
        self.assertTrue(
            all(row[2] == SURFACE_DETACHED_WORKTREE for row in residue),
            "residue rows carry the typed surface, not a free-form note",
        )


class DetachedResidueEnumeratorTest(unittest.TestCase):
    """``enumerate_detached_residue_for_repo`` — the public read of partitioned-out rows."""

    def _with_views(self, views, dispositions=None):
        original_views = source._active_lane_views
        original_disp = source._lifecycle_disposition_by_unit
        original_scope = source._repo_scope_workspace_id
        source._active_lane_views = lambda _root: tuple(views)
        source._lifecycle_disposition_by_unit = lambda: dict(dispositions or {})
        source._repo_scope_workspace_id = lambda _root: WORKSPACE
        try:
            return source.enumerate_detached_residue_for_repo(Path("/nonexistent"))
        finally:
            source._active_lane_views = original_views
            source._lifecycle_disposition_by_unit = original_disp
            source._repo_scope_workspace_id = original_scope

    def test_reports_residue_rows(self) -> None:
        rows, error = self._with_views(
            [_residue(WORKSPACE, "lane-a", "14001"), _live(WORKSPACE, "lane-b", "14002")]
        )
        self.assertIsNone(error)
        self.assertEqual(rows, (("14001", "lane-a", SURFACE_DETACHED_WORKTREE),))

    def test_enumeration_failure_is_an_error_not_an_empty_read(self) -> None:
        original = source._active_lane_views
        original_scope = source._repo_scope_workspace_id

        def _boom(_root):
            raise RuntimeError("inventory unreadable")

        source._active_lane_views = _boom
        source._repo_scope_workspace_id = lambda _root: WORKSPACE
        try:
            rows, error = source.enumerate_detached_residue_for_repo(Path("/nonexistent"))
        finally:
            source._active_lane_views = original
            source._repo_scope_workspace_id = original_scope
        self.assertEqual(rows, ())
        self.assertIsNotNone(error)
        self.assertIn("RuntimeError", error or "")


class ActiveRosterContractTest(unittest.TestCase):
    """The public roster enumerators keep their ``(roster, error)`` shape."""

    def test_enumerate_active_lanes_still_returns_roster_and_error(self) -> None:
        original_views = source._active_lane_views
        original_disp = source._lifecycle_disposition_by_unit
        source._active_lane_views = lambda _root: (
            _residue(WORKSPACE, "stale", "14001"),
            _live(WORKSPACE, "resident", "14002"),
        )
        source._lifecycle_disposition_by_unit = dict
        try:
            roster, error = source.enumerate_active_lanes(Path("/nonexistent"))
        finally:
            source._active_lane_views = original_views
            source._lifecycle_disposition_by_unit = original_disp
        self.assertIsNone(error)
        self.assertEqual(
            roster,
            (("14002", "resident"),),
            "the host-global roster returns bare (issue, lane) pairs, residue excluded",
        )


class CatalogSoftProfileDriftTest(unittest.TestCase):
    """The catalog's flow purpose must not describe a soft profile the doc no longer defines.

    #14813's second half: ``.mozyo-bridge/docs/catalog.yaml`` still summarised the flow as
    ``target 4 / burst 5 / stop 6+`` long after the doc moved to ``lane_count <= 10``
    (#13489 j#74982). ``docs validate`` is green on that — it checks refs and coverage, not
    whether a summary still means what the summarised doc says — so the drift was invisible.

    Checked against the doc rather than against a constant in this file: a hardcoded cap here
    would just become a third place to drift.
    """

    CATALOG = ROOT / ".mozyo-bridge" / "docs" / "catalog.yaml"
    FLOW_DOC = ROOT / "vibes" / "docs" / "logics" / "coordinator-sublane-development-flow.md"
    #: The retired vocabulary. Its shape, not one spelling, so a re-worded triple still trips.
    RETIRED_TRIPLE = __import__("re").compile(r"target\s*\d+\s*/\s*burst\s*\d+\s*/\s*stop\s*\d+")

    ENTRY_ID = "logic-coordinator-sublane-development-flow"

    def _flow_purpose(self) -> str:
        """The ``purpose:`` of the flow doc's own catalog entry.

        Scoped to the entry that declares ``id: <ENTRY_ID>`` and reads until the next entry —
        several other entries mention the soft profile as a pointer, so selecting by keyword
        would check the wrong line.
        """
        lines = self.CATALOG.read_text(encoding="utf-8").splitlines()
        start = None
        for index, line in enumerate(lines):
            if line.strip() == f"- id: {self.ENTRY_ID}":
                start = index
                break
        self.assertIsNotNone(start, f"catalog has no entry '{self.ENTRY_ID}'")
        assert start is not None  # narrowed for type readers
        for line in lines[start + 1 :]:
            if line.lstrip().startswith("- id:"):
                break
            stripped = line.strip()
            if stripped.startswith("purpose:"):
                return stripped
        self.fail(f"catalog entry '{self.ENTRY_ID}' has no purpose:")
        raise AssertionError  # pragma: no cover - self.fail raises

    def test_catalog_purpose_does_not_carry_the_retired_soft_profile_triple(self) -> None:
        purpose = self._flow_purpose()
        self.assertIsNone(
            self.RETIRED_TRIPLE.search(purpose),
            "the catalog still summarises the flow with the pre-#13489 target/burst/stop "
            f"triple: {purpose[:200]!r}",
        )

    def test_catalog_purpose_names_the_cap_the_flow_doc_defines(self) -> None:
        purpose = self._flow_purpose()
        # Anchored to the heading at start-of-line: the doc also *cites* this section inline
        # ("(`### Local Soft Profile`)"), and a substring split lands on the citation.
        lines = self.FLOW_DOC.read_text(encoding="utf-8").splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.startswith("### Local Soft Profile")),
            None,
        )
        self.assertIsNotNone(start, "the flow doc lost its '### Local Soft Profile' section")
        assert start is not None  # narrowed for type readers
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")), len(lines)
        )
        section = "\n".join(lines[start:end])
        cap = "lane_count <= 10"
        self.assertIn(
            cap,
            section,
            "the flow doc no longer states this cap; re-derive the catalog summary from the "
            "doc instead of updating this test",
        )
        self.assertIn(
            cap,
            purpose,
            "the catalog summary must name the same cap the flow doc defines; a summary that "
            "outlives its source is exactly the drift docs validate cannot see",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class GlanceCliSurfaceTest(unittest.TestCase):
    """R1-F2 / R1-F1: what the operator actually sees when running `workflow glance`.

    The first revision fixed the fold and asserted foreign exclusion through the **private**
    `_fold(..., workspace_id=...)`. That test could not fail on the real defect: the CLI called
    the host-global enumerator, so a foreign live lane still reached the rows (j#96246 measured
    host-global 13 vs current-workspace 12, `#14648` leaking). These tests go through
    `cmd_workflow_glance` instead, so the wiring is what is pinned, not the helper.
    """

    def _run_glance(self, *, roster, roster_error=None, residue=(), residue_error=None,
                    snapshots=()):
        import argparse
        import io
        import json as _json
        from contextlib import redirect_stdout

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow_glance as cli,
        )

        originals = {
            "roster": cli.enumerate_active_lanes_for_repo,
            "residue": cli.enumerate_detached_residue_for_repo,
            "diag": cli.enumerate_lifecycle_diagnostic,
            "snaps": cli.active_lane_snapshots,
        }
        cli.enumerate_active_lanes_for_repo = lambda _root: (tuple(roster), roster_error)
        cli.enumerate_detached_residue_for_repo = lambda _root: (tuple(residue), residue_error)
        cli.enumerate_lifecycle_diagnostic = lambda _root: ((), None)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
            GlanceCollection,
        )

        cli.active_lane_snapshots = lambda *a, **k: GlanceCollection(tuple(snapshots))
        args = argparse.Namespace(active_lanes=True, as_json=True, issue=None)
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                cli.cmd_workflow_glance(args)
        finally:
            cli.enumerate_active_lanes_for_repo = originals["roster"]
            cli.enumerate_detached_residue_for_repo = originals["residue"]
            cli.enumerate_lifecycle_diagnostic = originals["diag"]
            cli.active_lane_snapshots = originals["snaps"]
        return _json.loads(buffer.getvalue())

    def _snapshot(self, issue: str, *, issue_open: bool = True):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            LaneSignal,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (  # noqa: E501
            IssueGlanceSnapshot,
        )

        return IssueGlanceSnapshot(
            issue_id=issue,
            signal=LaneSignal(issue=issue, issue_open=issue_open),
            subject=f"subject {issue}",
            lane=f"lane-{issue}",
        )

    def test_cli_reads_the_repo_scoped_roster(self) -> None:
        # The wiring itself: the CLI must call the repo-scoped enumerator. Pinned by name
        # because the host-global one returns a superset and the difference is the leak.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow_glance as cli,
        )

        self.assertTrue(
            hasattr(cli, "enumerate_active_lanes_for_repo"),
            "the glance CLI must resolve the repo workspace scope; the host-global roster "
            "carries other workspaces' lanes into this repo's rows",
        )

    def test_detached_residue_reaches_the_json_surface(self) -> None:
        payload = self._run_glance(
            roster=(("14100", "resident"),),
            residue=(("14001", "stale", SURFACE_DETACHED_WORKTREE),),
            snapshots=(self._snapshot("14100"),),
        )
        self.assertEqual(
            payload["detached_residue"],
            [
                {
                    "issue": "14001",
                    "lane": "stale",
                    "execution_surface": SURFACE_DETACHED_WORKTREE,
                }
            ],
            "residue dropped from capacity must still be reported; otherwise the fix trades "
            "'residue counted as capacity' for 'detached worktree invisible'",
        )

    def test_residue_enumeration_error_degrades_the_view(self) -> None:
        payload = self._run_glance(
            roster=(),
            residue=(),
            residue_error="detached residue scope unresolved (repo workspace id unknown)",
        )
        self.assertTrue(
            payload["degraded"],
            "a residue read that could not run must degrade the view, never read as 'none'",
        )

    def test_closed_issue_is_reported_as_debt_not_as_an_active_row(self) -> None:
        payload = self._run_glance(
            roster=(("14100", "resident"), ("13820", "closed-lane")),
            snapshots=(
                self._snapshot("14100"),
                self._snapshot("13820", issue_open=False),
            ),
        )
        # `rows` uses the row payload's own key (`issue_id`); the debt list is this
        # commit's own shape and uses `issue`, matching `detached_residue`.
        active_ids = {row["issue_id"] for row in payload["rows"]}
        debt_ids = {row["issue"] for row in payload["closed_coordinator_debt"]}
        self.assertEqual(
            active_ids,
            {"14100"},
            "a closed issue is coordinator debt, not active workflow",
        )
        self.assertEqual(
            debt_ids,
            {"13820"},
            "closed debt must stay visible on its own surface, not be deleted",
        )


class ClosedStatusSurvivesTheNoGatePathTest(unittest.TestCase):
    """R2-F1: a Redmine-closed issue with no recognized Gate must not read as open.

    `active_lane_snapshots` knew `record.issue_open=False`, reported it in the notes, then
    built the fallback `LaneSignal(issue=issue)` — whose `issue_open` defaults to True. The
    closed fact was overwritten on the way to the partition, so `workflow glance` listed
    `14613` / `13820` as active rows while the same payload's notes called them closed
    (j#96286 live observation). Carrying the observed status asserts nothing about retirement:
    the row stays `degraded` because gate/commit/integration are still unresolved.

    Runs through `active_lane_snapshots` and `cmd_workflow_glance`, not the fold, because the
    reversal happened in the snapshot builder and only shows up on the CLI surface.
    """

    def _record(self, issue: str, *, issue_open: bool):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
            GlanceIssueRecord,
        )

        # No journals -> no canonical Gate resolves -> the degraded fallback path.
        return GlanceIssueRecord(
            issue_id=issue, subject=f"subject {issue}", journals=(), issue_open=issue_open
        )

    class _Source:
        def __init__(self, records):
            self._records = records

        def read_issue(self, issue):
            return self._records.get(issue)

    def test_snapshot_keeps_the_closed_status_without_a_gate(self) -> None:
        records = {"13820": self._record("13820", issue_open=False)}
        collection = source.active_lane_snapshots(
            (("13820", "closed-lane"),), redmine_source=self._Source(records)
        )
        self.assertEqual(len(collection.snapshots), 1)
        self.assertFalse(
            collection.snapshots[0].signal.issue_open,
            "the observed closed status must survive the no-Gate fallback; defaulting to open "
            "is what kept closed lanes in active capacity",
        )
        self.assertTrue(
            collection.degraded,
            "carrying the status must not silence the degraded/unknown report — gate, commit "
            "and integration facts are still unresolved (j#74323 Finding 3)",
        )

    def test_open_issue_without_a_gate_is_unchanged(self) -> None:
        records = {"14100": self._record("14100", issue_open=True)}
        collection = source.active_lane_snapshots(
            (("14100", "open-lane"),), redmine_source=self._Source(records)
        )
        self.assertTrue(collection.snapshots[0].signal.issue_open)

    def test_no_redmine_source_still_defaults_to_open(self) -> None:
        # Never observed is not the same as observed-closed: with no source at all the
        # conservative default stands.
        collection = source.active_lane_snapshots((("14100", "lane"),))
        self.assertTrue(collection.snapshots[0].signal.issue_open)

    def test_closed_lane_reaches_the_debt_surface_end_to_end(self) -> None:
        import argparse
        import io
        import json as _json
        from contextlib import redirect_stdout

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow_glance as cli,
        )

        records = {
            "13820": self._record("13820", issue_open=False),
            "14100": self._record("14100", issue_open=True),
        }
        originals = {
            "roster": cli.enumerate_active_lanes_for_repo,
            "residue": cli.enumerate_detached_residue_for_repo,
            "diag": cli.enumerate_lifecycle_diagnostic,
            "redmine": cli._redmine_source,
        }
        cli.enumerate_active_lanes_for_repo = lambda _root: (
            (("13820", "closed-lane"), ("14100", "open-lane")),
            None,
        )
        cli.enumerate_detached_residue_for_repo = lambda _root: ((), None)
        cli.enumerate_lifecycle_diagnostic = lambda _root: ((), None)
        cli._redmine_source = lambda _args: self._Source(records)
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                cli.cmd_workflow_glance(
                    argparse.Namespace(active_lanes=True, as_json=True, issue=None)
                )
        finally:
            cli.enumerate_active_lanes_for_repo = originals["roster"]
            cli.enumerate_detached_residue_for_repo = originals["residue"]
            cli.enumerate_lifecycle_diagnostic = originals["diag"]
            cli._redmine_source = originals["redmine"]
        payload = _json.loads(buffer.getvalue())
        self.assertEqual(
            {row["issue_id"] for row in payload["rows"]},
            {"14100"},
            "a Redmine-closed lane must not stay in the active rows the fill decision reads",
        )
        self.assertEqual(
            {row["issue"] for row in payload["closed_coordinator_debt"]},
            {"13820"},
            "it belongs on the debt surface instead — visible, but not counted",
        )


class ScopeUnresolvedDegradesTest(unittest.TestCase):
    """The fail-closed contract j#96306 option (b) preserved: no scope -> no roster, and say so.

    An unresolvable repo workspace must NOT fall back to the host-global roster: "no workspace
    registry / anchor" is not evidence that no foreign workspace exists, and a fresh checkout
    whose repo config selects herdr would re-import the very leak #14813 R1-F1 closed. So both
    reads report an error, which the CLI turns into `degraded`.

    Added because a probe found the contract unguarded: deleting the scope-unresolved error
    left every test green. `test_residue_enumeration_error_degrades_the_view` covers the CLI's
    handling of an error, not the production of one.
    """

    def _with_unresolved_scope(self, fn):
        original = source._repo_scope_workspace_id
        source._repo_scope_workspace_id = lambda _root: None
        try:
            return fn()
        finally:
            source._repo_scope_workspace_id = original

    def test_roster_reports_an_error_instead_of_falling_back(self) -> None:
        rows, error = self._with_unresolved_scope(
            lambda: source.enumerate_active_lanes_for_repo(Path("/nonexistent"))
        )
        self.assertEqual(rows, (), "an unresolvable scope must not yield a host-global roster")
        self.assertIsNotNone(
            error,
            "the caller has to be able to report degraded; a silent empty read is "
            "indistinguishable from 'nothing active'",
        )
        self.assertIn("scope unresolved", error or "")

    def test_residue_reports_an_error_instead_of_falling_back(self) -> None:
        rows, error = self._with_unresolved_scope(
            lambda: source.enumerate_detached_residue_for_repo(Path("/nonexistent"))
        )
        self.assertEqual(rows, ())
        self.assertIsNotNone(error)
        self.assertIn("scope unresolved", error or "")
