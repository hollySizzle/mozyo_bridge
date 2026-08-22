"""Live wiring of ``workflow version-track``'s Version × lane join (Redmine #15844).

Several real collaborators, hermetic: a real :class:`LaneLifecycleStore` in a temp home,
the real read-only lifecycle loader, the real workspace-scope resolver (only its herdr
segment seam is faked), and the real pure classifier. Only the Redmine side is supplied as
a bucket, since a live read is out of scope for a hermetic test.
"""

import ast
import importlib
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_version_track import (  # noqa: E501
    VersionTrackingUnavailable,
    gather_version_tracking,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.version_lane_tracking import (  # noqa: E501
    DISPOSITION_DRAIN_OWED,
    DISPOSITION_IN_FLIGHT,
    DISPOSITION_SETTLED,
)

WORKSPACE = "wVersionTrack"
OTHER_WORKSPACE = "wSomeoneElse"

_APPLICATION_MODULE = (
    "mozyo_bridge.e_110_execution_platform"
    ".f_140_delegated_coordinator_nested_handoff"
    ".application.cli_workflow_version_track"
)


@dataclass(frozen=True)
class _BucketIssue:
    issue_id: str
    is_closed: bool = False
    is_leaf: bool = True
    tracker: Optional[str] = "開発"
    status_name: Optional[str] = "未着手"
    parent_id: Optional[str] = None


@dataclass(frozen=True)
class _Bucket:
    issues: tuple


class _JoinBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mozyo-15844-join-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()

    def _store(self):
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore

        return LaneLifecycleStore(home=self.home)

    def _declare(self, workspace: str, lane: str, issue: str):
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            DecisionPointer,
            LaneLifecycleKey,
        )

        key = LaneLifecycleKey(workspace, lane)
        self._store().declare_active(
            key,
            decision=DecisionPointer(
                source="redmine", issue_id=issue, journal_id="109958"
            ),
            issue_id=issue,
        )
        return key

    def _terminalize(self, key, issue: str, *, target: str):
        """Drive the row to a terminal disposition through the real CAS.

        The outcome is asserted: a bound lane may only be decided by a record filed on
        its own issue, so a mismatched pointer is refused zero-write. Leaving that
        unchecked would let a refused CAS masquerade as a terminalized lane and quietly
        turn the "terminal lanes are not owed" assertions vacuous.
        """
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            DISPOSITION_ACTIVE,
            DecisionPointer,
        )

        outcome = self._store().transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=target,
            decision=DecisionPointer(
                source="redmine", issue_id=issue, journal_id="109958"
            ),
        )
        self.assertTrue(
            outcome.applied, f"terminalizing {key} to {target} was refused: {outcome}"
        )

    def _gather(self, issues, *, workspace=WORKSPACE):
        segment = mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider"
            ".application.herdr_session_start.herdr_workspace_segment",
            return_value=workspace,
        )
        env = mock.patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        with segment, env:
            return gather_version_tracking(
                self.repo,
                home=self.home,
                bucket=_Bucket(issues=tuple(issues)),
                resolved_version=("329", "v2.2.0 ハーネス/運用整備"),
            )

    def _disposition(self, snapshot, issue_id):
        return next(
            row.disposition for row in snapshot.issues if row.issue_id == issue_id
        )


class DrainOwedJoinTest(_JoinBase):
    def test_closed_issue_with_a_live_lane_row_is_drain_owed(self):
        """The #15789 shape, joined from a real lifecycle store.

        Measured on this host 2026-08-22: Version #329 held exactly this shape twice
        (#15842 / #15843 — both integrated into main, both lane rows still ``active``),
        and no other surface could see it, because ``dispatch-plan`` projects the same
        Version onto its OPEN leaf issues.
        """
        self._declare(WORKSPACE, "issue_15842_x", "15842")
        snapshot = self._gather(
            [_BucketIssue("15842", is_closed=True, status_name="クローズ")]
        )
        self.assertEqual(self._disposition(snapshot, "15842"), DISPOSITION_DRAIN_OWED)
        self.assertEqual([r.issue_id for r in snapshot.attention], ["15842"])

    def test_retired_lane_of_a_closed_issue_is_settled(self):
        key = self._declare(WORKSPACE, "issue_15841_x", "15841")
        self._terminalize(key, "15841", target="retired")
        snapshot = self._gather(
            [_BucketIssue("15841", is_closed=True, status_name="クローズ")]
        )
        self.assertEqual(self._disposition(snapshot, "15841"), DISPOSITION_SETTLED)
        self.assertEqual(snapshot.attention, ())

    def test_superseded_lane_of_a_closed_issue_is_settled(self):
        key = self._declare(WORKSPACE, "issue_15693_x", "15693")
        self._terminalize(key, "15693", target="superseded")
        snapshot = self._gather(
            [_BucketIssue("15693", is_closed=True, status_name="クローズ")]
        )
        self.assertEqual(self._disposition(snapshot, "15693"), DISPOSITION_SETTLED)

    def test_open_issue_with_a_live_lane_is_in_flight(self):
        self._declare(WORKSPACE, "issue_15844_x", "15844")
        snapshot = self._gather([_BucketIssue("15844", status_name="未着手")])
        self.assertEqual(self._disposition(snapshot, "15844"), DISPOSITION_IN_FLIGHT)

    def test_another_workspace_lane_never_joins(self):
        # The lifecycle store is host-global; reporting another project's lane would
        # invite acting on it (the scoping `reboot-audit` already uses).
        self._declare(OTHER_WORKSPACE, "issue_15842_x", "15842")
        snapshot = self._gather(
            [_BucketIssue("15842", is_closed=True, status_name="クローズ")]
        )
        self.assertEqual(self._disposition(snapshot, "15842"), DISPOSITION_SETTLED)
        self.assertEqual(snapshot.unscoped_lanes, ())


class UnscopedLaneTest(_JoinBase):
    def test_nonterminal_lane_outside_the_version_is_surfaced(self):
        """Version scoping creates a blind spot; the surface is what keeps it visible."""
        self._declare(WORKSPACE, "issue_15110_x", "15110")
        snapshot = self._gather([_BucketIssue("15844")])
        self.assertEqual(
            [(lane.lane_id, lane.issue_id) for lane in snapshot.unscoped_lanes],
            [("issue_15110_x", "15110")],
        )

    def test_terminal_lane_outside_the_version_is_not_surfaced(self):
        # Nothing is owed on it, so listing it would only dilute the section.
        key = self._declare(WORKSPACE, "issue_15110_x", "15110")
        self._terminalize(key, "15110", target="retired")
        snapshot = self._gather([_BucketIssue("15844")])
        self.assertEqual(snapshot.unscoped_lanes, ())

    def test_lane_of_another_workspace_is_not_surfaced_as_unscoped(self):
        self._declare(OTHER_WORKSPACE, "issue_15110_x", "15110")
        snapshot = self._gather([_BucketIssue("15844")])
        self.assertEqual(snapshot.unscoped_lanes, ())

    def test_in_version_lane_is_not_also_reported_as_unscoped(self):
        self._declare(WORKSPACE, "issue_15844_x", "15844")
        snapshot = self._gather([_BucketIssue("15844")])
        self.assertEqual(snapshot.unscoped_lanes, ())


class UnreadableAuthorityTest(_JoinBase):
    def test_unresolvable_workspace_identity_is_unavailable_not_empty(self):
        """An unresolvable identity would make the scope filter match nothing, which
        renders as "this Version owns no lanes" — indistinguishable from a Version that
        genuinely owns none (the fail-open ``reboot-audit`` review j#89191 finding 4
        removed from the adjacent surface)."""
        self._declare(WORKSPACE, "issue_15842_x", "15842")
        with self.assertRaises(VersionTrackingUnavailable) as caught:
            self._gather(
                [_BucketIssue("15842", is_closed=True, status_name="クローズ")],
                workspace="",
            )
        self.assertIn("workspace identity", str(caught.exception))

    def test_unreadable_lifecycle_store_is_unavailable_not_empty(self):
        """``load_lane_lifecycle_readonly`` returns None for its fail-closed cases and
        ``()`` only for a genuinely absent store. Folding the two together would report
        an unreadable lane authority as "nothing is owed"."""
        loader = mock.patch(
            "mozyo_bridge.core.state.lane_lifecycle_readonly"
            ".load_lane_lifecycle_readonly",
            return_value=None,
        )
        with loader, self.assertRaises(VersionTrackingUnavailable) as caught:
            self._gather([_BucketIssue("15844")])
        self.assertIn("lifecycle store", str(caught.exception))

    def test_absent_store_is_an_empty_result_not_an_unavailable_one(self):
        # The other side of the same line: nothing declared is a legitimate snapshot.
        snapshot = self._gather([_BucketIssue("15844")])
        self.assertEqual(len(snapshot.issues), 1)
        self.assertEqual(snapshot.unscoped_lanes, ())


class LazyImportResolutionTest(unittest.TestCase):
    """Every deferred import in the live wiring actually resolves.

    This module defers its collaborator imports into function bodies, and Python does not
    check a function-local ``from X import Y`` until that line runs. A rename on the far
    side therefore survives every test that fakes the seam and only fails in production —
    the exact shape #15745 j#109007 measured, where 117 green tests sat on top of a live
    adapter that raised ``ImportError`` at send time.
    """

    def test_every_import_in_the_module_resolves(self):
        module = importlib.import_module(_APPLICATION_MODULE)
        source = Path(module.__file__).read_text(encoding="utf-8")
        checked = 0
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.level:  # no relative imports in this package layout
                continue
            target = importlib.import_module(node.module)
            for alias in node.names:
                with self.subTest(module=node.module, name=alias.name):
                    self.assertTrue(
                        hasattr(target, alias.name),
                        f"{node.module} has no attribute {alias.name!r}",
                    )
                checked += 1
        self.assertGreater(checked, 0, "the import scan found nothing to check")

    def test_the_scan_covers_the_function_local_imports(self):
        # Guards the guard: if the module's deferred imports were ever hoisted to the top
        # this test still passes, but a scan that silently stopped seeing them would not.
        source = Path(
            importlib.import_module(_APPLICATION_MODULE).__file__
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level = {id(n) for n in tree.body}
        deferred = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and id(n) not in top_level
        ]
        self.assertTrue(deferred, "expected deferred imports in the live wiring")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
