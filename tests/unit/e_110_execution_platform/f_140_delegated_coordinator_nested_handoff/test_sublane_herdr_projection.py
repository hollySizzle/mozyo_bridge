"""herdr `sublane list` projection tests (Redmine #13377 shared project workspace).

Pins the pure fold: one :class:`SublaneLaneView` per lane unit ``(workspace_id,
lane_id)`` — non-default lanes are sublanes, a registry workspace's default-lane
coordinator pair never is, and a legacy pre-#13331 per-lane ``wt_...`` workspace's
default-lane pair stays visible as the compatibility read. Identity joins from the
lane metadata records; hints are advisory-only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    GATEWAY_SLOT_MISSING_HINT,
    LANE_RECORD_MISSING_HINT,
    LANE_SLOTS_MISSING_HINT,
    WORKER_SLOT_MISSING_HINT,
    probe_worktree_resolved,
    project_herdr_sublanes,
)
from mozyo_bridge.core.state.lane_metadata import (
    LANE_STATUS_RETIRED,
    LaneMetadataRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    STALE_HINT_DUPLICATE_ISSUE_LANE,
    STALE_HINT_WORKTREE_UNRESOLVED,
    SUBLANE_STATE_ACTIVE,
    SUBLANE_STATE_DETACHED,
    SUBLANE_STATE_GATEWAY_ONLY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class ProjectHerdrSublanesTest(unittest.TestCase):
    def test_folds_lane_units_excludes_coordinator_pairs(self) -> None:
        records = {
            "wt_aaaa": LaneMetadataRecord(
                lane_workspace_token="wt_aaaa",
                repo_workspace_id="wsMain",
                issue_id="101",
                lane_label="issue_101_alpha",
                worktree_path="/work/mozyo_bridge_issue_101_alpha",
                lane_id="issue_101_alpha",
            ),
            "wt_bbbb": LaneMetadataRecord(
                lane_workspace_token="wt_bbbb",
                repo_workspace_id="wsMain",
                issue_id="202",
                lane_label="issue_202_beta",
                worktree_path="/work/mozyo_bridge_issue_202_beta",
                lane_id="issue_202_beta",
            ),
        }
        rows = [
            # shared project workspace lane units (#13377): sublanes
            _row("wsMain", "codex", "issue_101_alpha", "w2:p4"),
            _row("wsMain", "claude", "issue_101_alpha", "w2:p5"),
            _row("wsMain", "codex", "issue_202_beta", "w2:p6"),
            _row("wsMain", "claude", "issue_202_beta", "w2:p7"),
            # the project's default-lane coordinator pair — never a sublane
            _row("wsMain", "codex", "", "w2:p3"),
            _row("wsMain", "claude", "", "w2:p2"),
            # a FOREIGN project's coordinator pair — also never a sublane
            _row("wsOther", "codex", "", "w9:p2"),
            _row("wsOther", "claude", "", "w9:p3"),
            # a foreign non-mzb1 agent — dropped
            {"name": "someones-shell", "pane_id": "wZ:p1"},
        ]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records=records,
            repo_workspace_id="wsMain",
        )
        self.assertEqual(
            [(v.workspace_id, v.lane_id) for v in views],
            [("wsMain", "issue_101_alpha"), ("wsMain", "issue_202_beta")],
        )
        a, b = views
        self.assertEqual(a.lane_label, "issue_101_alpha")
        self.assertEqual(a.issue, "101")
        self.assertEqual(a.repo_root, "/work/mozyo_bridge_issue_101_alpha")
        self.assertEqual(a.gateway_pane, "w2:p4")
        self.assertEqual(a.worker_pane, "w2:p5")
        self.assertEqual(a.state, SUBLANE_STATE_ACTIVE)
        self.assertEqual(b.issue, "202")

    def test_legacy_lane_workspace_stays_visible(self) -> None:
        # A pre-#13377 lane: its own wt_ workspace, default-lane pair (compat read).
        rows = [
            _row("wt_1234", "codex", "", "wL1:p2"),
            _row("wt_1234", "claude", "", "wL1:p3"),
        ]
        views = project_herdr_sublanes(
            rows, exclude_workspace_id="wsMain", resolve_repo_root=lambda ws: None
        )
        self.assertEqual([(v.workspace_id, v.lane_id) for v in views], [("wt_1234", "default")])

    def test_recordless_lane_unit_falls_back_to_lane_id_label(self) -> None:
        views = project_herdr_sublanes(
            [_row("wsMain", "codex", "issue_303_gamma", "w2:p8")],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
        )
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].gateway_pane, "w2:p8")
        self.assertIsNone(views[0].worker_pane)
        self.assertEqual(views[0].state, SUBLANE_STATE_GATEWAY_ONLY)
        # The lane segment is the requested lane label at create — the honest fallback.
        self.assertEqual(views[0].lane_label, "issue_303_gamma")
        self.assertEqual(views[0].issue, "303")
        self.assertIn(LANE_RECORD_MISSING_HINT, views[0].stale_hints)

    def test_row_without_locator_is_dropped(self) -> None:
        rows = [
            {"name": encode_assigned_name("wsMain", "codex", "issue_1_x"), "pane_id": ""},
            _row("wsMain", "claude", "issue_1_x", "wL1:p3"),
        ]
        views = project_herdr_sublanes(
            rows, exclude_workspace_id="", resolve_repo_root=lambda ws: None
        )
        self.assertEqual(len(views), 1)
        self.assertIsNone(views[0].gateway_pane)
        self.assertEqual(views[0].worker_pane, "wL1:p3")

    # -- lane metadata record join (Redmine #13356 j#73386) --------------------

    def test_lane_record_resolves_wt_token_to_human_identity(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc123",
            issue_id="13356",
            lane_label="issue_13356_cockpit_aggregate",
            branch="issue_13356_cockpit_aggregate",
            worktree_path="/work/mozyo_bridge_issue_13356_cockpit_aggregate",
        )
        rows = [
            _row("wt_abc123", "codex", "", "wD:p2"),
            _row("wt_abc123", "claude", "", "wD:p3"),
        ]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="wsMain",
            # A wt_<hash> token is never registry-resolvable (#13331 j#73357).
            resolve_repo_root=lambda ws: None,
            resolve_lane_record={"wt_abc123": record}.get,
        )
        self.assertEqual(len(views), 1)
        view = views[0]
        self.assertEqual(view.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(view.issue, "13356")
        self.assertEqual(view.branch, "issue_13356_cockpit_aggregate")
        self.assertEqual(
            view.repo_root, "/work/mozyo_bridge_issue_13356_cockpit_aggregate"
        )
        self.assertEqual(view.stale_hints, ())

    def test_missing_lane_record_degrades_to_raw_token_with_hint(self) -> None:
        rows = [_row("wt_orphan", "codex", "", "wX:p2")]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="",
            resolve_repo_root=lambda ws: None,
            resolve_lane_record=lambda ws: None,
        )
        self.assertEqual(len(views), 1)
        # Fail-open degrade: the raw token stays the label, kept visible via the
        # machine-readable hint — never a guessed identity, never a crash.
        self.assertEqual(views[0].lane_label, "wt_orphan")
        self.assertIsNone(views[0].issue)
        self.assertIn(LANE_RECORD_MISSING_HINT, views[0].stale_hints)

    def test_record_issue_falls_back_to_label_convention(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_x",
            issue_id="",
            lane_label="issue_777_slug",
        )
        rows = [_row("wt_x", "claude", "", "wY:p3")]
        views = project_herdr_sublanes(
            rows,
            exclude_workspace_id="",
            resolve_repo_root=lambda ws: None,
            resolve_lane_record={"wt_x": record}.get,
        )
        self.assertEqual(views[0].issue, "777")


class HerdrStaleHintsTest(unittest.TestCase):
    """The #13358 herdr stale / retire hint supply (advisory-only display material)."""

    def test_lost_worker_slot_raises_worker_slot_missing(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_a",
            issue_id="101",
            lane_label="issue_101_alpha",
        )
        views = project_herdr_sublanes(
            [_row("wt_a", "codex", "", "wL1:p2")],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_a": record},
        )
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].state, SUBLANE_STATE_GATEWAY_ONLY)
        self.assertEqual(views[0].stale_hints, (WORKER_SLOT_MISSING_HINT,))

    def test_lost_gateway_slot_raises_gateway_slot_missing(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_a",
            issue_id="101",
            lane_label="issue_101_alpha",
        )
        views = project_herdr_sublanes(
            [_row("wt_a", "claude", "", "wL1:p3")],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_a": record},
        )
        self.assertEqual(views[0].stale_hints, (GATEWAY_SLOT_MISSING_HINT,))

    def test_intact_lane_has_no_slot_hints(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_a",
            issue_id="101",
            lane_label="issue_101_alpha",
        )
        views = project_herdr_sublanes(
            [
                _row("wt_a", "codex", "", "wL1:p2"),
                _row("wt_a", "claude", "", "wL1:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_a": record},
        )
        self.assertEqual(views[0].stale_hints, ())

    def test_active_record_without_live_slot_is_vanished_workspace_row(self) -> None:
        gone = LaneMetadataRecord(
            lane_workspace_token="wt_gone",
            repo_workspace_id="wsMain",
            issue_id="303",
            lane_label="issue_303_gone",
            branch="issue_303_gone",
            worktree_path="/work/mozyo_bridge_issue_303_gone",
        )
        live = LaneMetadataRecord(
            lane_workspace_token="wt_live",
            repo_workspace_id="wsMain",
            issue_id="101",
            lane_label="issue_101_alpha",
        )
        views = project_herdr_sublanes(
            [
                _row("wt_live", "codex", "", "wL1:p2"),
                _row("wt_live", "claude", "", "wL1:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_live": live, "wt_gone": gone},
            repo_workspace_id="wsMain",
        )
        # The vanished lane is appended after the live lanes as a detached row —
        # visible instead of silently dropping out of `sublane list`.
        self.assertEqual([v.workspace_id for v in views], ["wt_live", "wt_gone"])
        vanished = views[1]
        self.assertEqual(vanished.lane_label, "issue_303_gone")
        self.assertEqual(vanished.issue, "303")
        self.assertEqual(vanished.state, SUBLANE_STATE_DETACHED)
        self.assertIsNone(vanished.gateway_pane)
        self.assertIsNone(vanished.worker_pane)
        self.assertEqual(vanished.stale_hints, (LANE_SLOTS_MISSING_HINT,))
        self.assertEqual(views[0].stale_hints, ())

    def test_retired_tombstone_never_becomes_vanished_row(self) -> None:
        tombstone = LaneMetadataRecord(
            lane_workspace_token="wt_retired",
            repo_workspace_id="wsMain",
            issue_id="404",
            lane_label="issue_404_done",
            status=LANE_STATUS_RETIRED,
            retired_at="2026-07-08T00:00:00+00:00",
        )
        views = project_herdr_sublanes(
            [],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_retired": tombstone},
            repo_workspace_id="wsMain",
        )
        self.assertEqual(views, ())

    def test_own_workspace_record_never_becomes_vanished_row(self) -> None:
        own = LaneMetadataRecord(
            lane_workspace_token="wsMain",
            repo_workspace_id="wsMain",
            lane_label="main",
        )
        views = project_herdr_sublanes(
            [],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wsMain": own},
            repo_workspace_id="wsMain",
        )
        self.assertEqual(views, ())

    # -- repo scope (j#73459 finding 1): the record store is host-global ---------

    def test_foreign_repo_record_never_becomes_vanished_row(self) -> None:
        foreign = LaneMetadataRecord(
            lane_workspace_token="wt_other",
            repo_workspace_id="other_repo",
            issue_id="900",
            lane_label="issue_900_foreign",
        )
        views = project_herdr_sublanes(
            [],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_other": foreign},
            repo_workspace_id="wsMain",
        )
        self.assertEqual(views, ())

    def test_unscoped_caller_never_emits_vanished_rows(self) -> None:
        # No caller repo scope (empty) — even a record with an EMPTY
        # repo_workspace_id never matches: empty never fabricates attribution.
        unattributed = LaneMetadataRecord(
            lane_workspace_token="wt_gone",
            repo_workspace_id="",
            issue_id="303",
            lane_label="issue_303_gone",
        )
        views = project_herdr_sublanes(
            [],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_gone": unattributed},
        )
        self.assertEqual(views, ())

    def test_foreign_repo_lane_never_raises_duplicate_hint(self) -> None:
        # A live lane of ANOTHER repo carrying the same issue id must neither
        # raise nor receive duplicate_issue_lane against this repo's lane.
        records = {
            "wt_mine": LaneMetadataRecord(
                lane_workspace_token="wt_mine",
                repo_workspace_id="wsMain",
                issue_id="500",
                lane_label="issue_500_mine",
            ),
            "wt_theirs": LaneMetadataRecord(
                lane_workspace_token="wt_theirs",
                repo_workspace_id="other_repo",
                issue_id="500",
                lane_label="issue_500_theirs",
            ),
        }
        views = project_herdr_sublanes(
            [
                _row("wt_mine", "codex", "", "wL1:p2"),
                _row("wt_mine", "claude", "", "wL1:p3"),
                _row("wt_theirs", "codex", "", "wL2:p2"),
                _row("wt_theirs", "claude", "", "wL2:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records=records,
            repo_workspace_id="wsMain",
        )
        self.assertEqual([v.stale_hints for v in views], [(), ()])

    def test_duplicate_issue_lanes_name_each_peer(self) -> None:
        records = {
            "wt_a": LaneMetadataRecord(
                lane_workspace_token="wt_a",
                repo_workspace_id="wsMain",
                issue_id="500",
                lane_label="issue_500_first",
            ),
            "wt_b": LaneMetadataRecord(
                lane_workspace_token="wt_b",
                repo_workspace_id="wsMain",
                issue_id="500",
                lane_label="issue_500_second",
            ),
        }
        views = project_herdr_sublanes(
            [
                _row("wt_a", "codex", "", "wL1:p2"),
                _row("wt_a", "claude", "", "wL1:p3"),
                _row("wt_b", "codex", "", "wL2:p2"),
                _row("wt_b", "claude", "", "wL2:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records=records,
            repo_workspace_id="wsMain",
        )
        self.assertEqual(
            views[0].stale_hints,
            (f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_500_second",),
        )
        self.assertEqual(
            views[1].stale_hints,
            (f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_500_first",),
        )

    def test_vanished_record_duplicates_against_live_relaunch(self) -> None:
        # The #13360 lost-workspace relaunch shape: the issue's original lane
        # workspace vanished (record still active) and a relaunched live lane
        # carries the same issue — both rows flag each other.
        records = {
            "wt_old": LaneMetadataRecord(
                lane_workspace_token="wt_old",
                repo_workspace_id="wsMain",
                issue_id="600",
                lane_label="issue_600_lost",
            ),
            "wt_new": LaneMetadataRecord(
                lane_workspace_token="wt_new",
                repo_workspace_id="wsMain",
                issue_id="600",
                lane_label="issue_600_relaunch",
            ),
        }
        views = project_herdr_sublanes(
            [
                _row("wt_new", "codex", "", "wL1:p2"),
                _row("wt_new", "claude", "", "wL1:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records=records,
            repo_workspace_id="wsMain",
        )
        self.assertEqual([v.workspace_id for v in views], ["wt_new", "wt_old"])
        self.assertEqual(
            views[0].stale_hints,
            (f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_600_lost",),
        )
        self.assertEqual(
            views[1].stale_hints,
            (
                LANE_SLOTS_MISSING_HINT,
                f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:issue_600_relaunch",
            ),
        )

    def test_unresolved_worktree_probe_raises_hint(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_a",
            issue_id="101",
            lane_label="issue_101_alpha",
            worktree_path="/work/removed_checkout",
        )
        views = project_herdr_sublanes(
            [
                _row("wt_a", "codex", "", "wL1:p2"),
                _row("wt_a", "claude", "", "wL1:p3"),
            ],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            lane_records={"wt_a": record},
            worktree_resolved=lambda path: False,
        )
        self.assertEqual(views[0].stale_hints, (STALE_HINT_WORKTREE_UNRESOLVED,))

    def test_unknown_worktree_probe_never_fabricates_hint(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_a",
            issue_id="101",
            lane_label="issue_101_alpha",
            worktree_path="/work/somewhere",
        )
        for probe in (lambda path: None, lambda path: True):
            views = project_herdr_sublanes(
                [
                    _row("wt_a", "codex", "", "wL1:p2"),
                    _row("wt_a", "claude", "", "wL1:p3"),
                ],
                exclude_workspace_id="wsMain",
                resolve_repo_root=lambda ws: None,
                lane_records={"wt_a": record},
                worktree_resolved=probe,
            )
            self.assertEqual(views[0].stale_hints, ())

    def test_lane_without_worktree_path_never_probes(self) -> None:
        probed: list[str] = []

        def probe(path: str) -> bool:
            probed.append(path)
            return False

        views = project_herdr_sublanes(
            [_row("wt_orphan", "codex", "", "wX:p2")],
            exclude_workspace_id="wsMain",
            resolve_repo_root=lambda ws: None,
            resolve_lane_record=lambda ws: None,
            worktree_resolved=probe,
        )
        self.assertEqual(probed, [])
        # The record-missing degrade keeps its identity hint alongside the slot hint.
        self.assertEqual(
            views[0].stale_hints,
            (WORKER_SLOT_MISSING_HINT, LANE_RECORD_MISSING_HINT),
        )


class RepoScopeWorkspaceIdTest(unittest.TestCase):
    """The record-scoping key resolves the MAIN workspace identity (j#73469).

    A linked-worktree caller must scope by its INHERITED main workspace id (what
    ``sublane create`` stamped into the records), never by its own per-lane
    ``wt_<hash>`` segment.
    """

    _REGISTRY = "mozyo_bridge.core.state.workspace_registry"
    _SESSION_START = (
        "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
        "application.herdr_session_start"
    )

    def test_linked_worktree_resolves_main_identity(self) -> None:
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )

        main_root = Path("/work/main_checkout")
        with mock.patch(
            f"{self._REGISTRY}._main_worktree_root", return_value=main_root
        ), mock.patch(
            f"{self._SESSION_START}.herdr_workspace_segment",
            side_effect=lambda p: "wsMain" if p == main_root else "wt_lane",
        ):
            self.assertEqual(repo_scope_workspace_id(Path("/work/lane")), "wsMain")

    def test_main_checkout_resolves_itself(self) -> None:
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            repo_scope_workspace_id,
        )

        with mock.patch(
            f"{self._REGISTRY}._main_worktree_root", return_value=None
        ), mock.patch(
            f"{self._SESSION_START}.herdr_workspace_segment",
            return_value="wsMain",
        ):
            self.assertEqual(
                repo_scope_workspace_id(Path("/work/main_checkout")), "wsMain"
            )


class HerdrSublaneViewsLinkedWorktreeTest(unittest.TestCase):
    """Regression (j#73469 finding 1): a linked-worktree caller still scopes
    the CURRENT repo's records into the vanished / duplicate diagnosis."""

    _MODULE = (
        "mozyo_bridge.e_110_execution_platform."
        "f_140_delegated_coordinator_nested_handoff.application."
        "sublane_herdr_projection"
    )
    _REGISTRY = "mozyo_bridge.core.state.workspace_registry"
    _SESSION_START = (
        "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
        "application.herdr_session_start"
    )

    def test_linked_worktree_caller_still_emits_current_repo_vanished_row(
        self,
    ) -> None:
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            herdr_sublane_views,
        )

        lane_path = Path("/work/lane_worktree").expanduser().resolve()
        main_root = Path("/work/main_checkout")
        gone = LaneMetadataRecord(
            lane_workspace_token="wt_gone",
            # Stamped at create time with the MAIN checkout's identity.
            repo_workspace_id="wsMain",
            issue_id="303",
            lane_label="issue_303_gone",
        )

        def _segment(path):
            # The caller's own segment is a per-lane token; only the main
            # checkout resolves to the registry/anchor workspace id.
            return "wsMain" if path == main_root else "wt_caller_lane"

        with mock.patch(
            f"{self._SESSION_START}.herdr_workspace_segment", side_effect=_segment
        ), mock.patch(
            f"{self._REGISTRY}._main_worktree_root", return_value=main_root
        ), mock.patch(
            f"{self._MODULE}.list_herdr_agent_rows", return_value=[]
        ), mock.patch(
            "mozyo_bridge.core.state.lane_metadata.load_lane_records",
            return_value={"wt_gone": gone},
        ):
            views = herdr_sublane_views(lane_path)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].workspace_id, "wt_gone")
        self.assertEqual(views[0].stale_hints, (LANE_SLOTS_MISSING_HINT,))


class ProbeWorktreeResolvedTest(unittest.TestCase):
    """The live git-checkout probe's unknown / gone boundary (pure input shapes)."""

    def test_empty_path_is_unknown(self) -> None:
        self.assertIsNone(probe_worktree_resolved(""))

    def test_missing_directory_is_unresolved(self) -> None:
        self.assertIs(probe_worktree_resolved("/nonexistent/path/for/13358"), False)


class HerdrLaneViewForWorktreeTest(unittest.TestCase):
    """The lane-record-joined single-lane read-back (Redmine #13356 / #13377).

    Not wired into dispatch-worker here — the herdr dispatch drive is #13357's
    surface; this pins the seam it can adopt for a recorded identity check.
    """

    _MODULE = (
        "mozyo_bridge.e_110_execution_platform."
        "f_140_delegated_coordinator_nested_handoff.application."
        "sublane_herdr_projection"
    )

    #: The lane worktree path every test resolves; its stable metadata / legacy key.
    _WORKTREE = "/work/lane"

    @property
    def _token(self) -> str:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            derive_lane_workspace_token,
        )

        return derive_lane_workspace_token(
            str(Path(self._WORKTREE).expanduser().resolve())
        )

    def _resolve(self, *, segment, rows, records):
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            herdr_lane_view_for_worktree,
        )

        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_start.herdr_workspace_segment",
            return_value=segment,
        ), mock.patch(
            f"{self._MODULE}.list_herdr_agent_rows", return_value=rows
        ), mock.patch(
            "mozyo_bridge.core.state.lane_metadata.load_lane_records",
            return_value=records,
        ):
            return herdr_lane_view_for_worktree(self._WORKTREE)

    def test_resolves_shared_model_lane_unit_with_metadata_join(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token=self._token,
            repo_workspace_id="wsMain",
            issue_id="13377",
            lane_label="issue_13377_shared",
            branch="issue_13377_shared",
            lane_id="issue_13377_shared",
        )
        view = self._resolve(
            segment="wsMain",
            rows=[
                _row("wsMain", "codex", "issue_13377_shared", "w2:p4"),
                _row("wsMain", "claude", "issue_13377_shared", "w2:p5"),
            ],
            records={self._token: record},
        )
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, "wsMain")
        self.assertEqual(view.lane_id, "issue_13377_shared")
        self.assertEqual(view.lane_label, "issue_13377_shared")
        self.assertEqual(view.issue, "13377")
        self.assertEqual(view.gateway_pane, "w2:p4")
        self.assertEqual(view.worker_pane, "w2:p5")
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        self.assertEqual(view.stale_hints, ())

    def test_resolves_legacy_lane_via_token(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token=self._token,
            issue_id="13356",
            lane_label="issue_13356_cockpit_aggregate",
            branch="issue_13356_cockpit_aggregate",
        )
        view = self._resolve(
            segment="wsMain",
            rows=[
                _row(self._token, "codex", "", "wD:p2"),
                _row(self._token, "claude", "", "wD:p3"),
            ],
            records={self._token: record},
        )
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, self._token)
        self.assertEqual(view.lane_id, "default")
        self.assertEqual(view.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(view.issue, "13356")
        self.assertEqual(view.state, SUBLANE_STATE_ACTIVE)
        self.assertEqual(view.stale_hints, ())

    def test_missing_record_falls_back_to_legacy_unit_and_basename(self) -> None:
        view = self._resolve(
            segment="wsMain",
            rows=[_row(self._token, "claude", "", "wD:p3")],
            records={},
        )
        self.assertIsNotNone(view)
        self.assertEqual(view.workspace_id, self._token)
        self.assertEqual(view.lane_label, "lane")
        self.assertIn(LANE_RECORD_MISSING_HINT, view.stale_hints)

    def test_no_live_slot_resolves_none(self) -> None:
        view = self._resolve(segment="wsMain", rows=[], records={})
        self.assertIsNone(view)

    def test_unresolvable_segment_resolves_none(self) -> None:
        view = self._resolve(segment="", rows=[], records={})
        self.assertIsNone(view)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
