"""Active-capacity vs lifecycle-diagnostic roster split (Redmine #13681 W4).

Design Answer j#76630 required correction: a superseded lane still holding live panes
must not consume active capacity, yet must stay visible on a diagnostic surface. These
tests seed a REAL lifecycle store and drive `enumerate_active_lanes` over fake live
views, proving the disposition join excludes non-active lanes from capacity while
`enumerate_lifecycle_diagnostic` retains them — and that an unreadable / empty
lifecycle store leaves the roster byte-invariant (every live lane kept).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_herdr_projection as proj,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (  # noqa: E501
    enumerate_active_lanes,
    enumerate_lifecycle_diagnostic,
)

WS = "wProj"
ISSUE = "13583"
ORIG = "issue_13583_x"
REC = "issue_13583_recovery"


@dataclass
class _View:
    issue: str
    lane_label: str
    lane_id: str
    workspace_id: str
    state: str = "active"


def _decision(journal="76630") -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=journal)


class GlanceLifecycleRosterTest(unittest.TestCase):
    def _views(self):
        return [
            _View(ISSUE, ORIG, ORIG, WS),
            _View(ISSUE, REC, REC, WS),
        ]

    def _run(self, home, *, scope=WS):
        # R2-F3 (j#77292): the diagnostic is scoped to the current repo's workspace
        # segment; patch that resolution to WS so the seeded rows are in scope.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start as hss,
        )

        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
        ), patch.object(proj, "repo_backend_is_herdr", return_value=True), patch.object(
            proj, "herdr_sublane_views", return_value=self._views()
        ), patch.object(
            hss, "herdr_workspace_segment", return_value=scope
        ):
            active = enumerate_active_lanes(Path("."))
            diag = enumerate_lifecycle_diagnostic(Path("."))
        return active, diag

    def test_superseded_lane_excluded_from_capacity_but_kept_in_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            # Original owns the issue, then hands it to the recovery lane. The original
            # is now superseded but its live panes still appear in the view list.
            store.declare_active(
                LaneLifecycleKey(WS, ORIG), decision=_decision(), issue_id=ISSUE
            )
            out = store.supersede_and_activate(
                superseded=LaneLifecycleKey(WS, ORIG),
                expected_revision=1,
                recovery=LaneLifecycleKey(WS, REC),
                decision=_decision("76631"),
            )
            self.assertTrue(out.applied)

            (roster, err), (diag, diag_err) = self._run(home)
            self.assertIsNone(err)
            # Capacity roster: only the active recovery lane — the superseded original
            # no longer consumes capacity even though its panes are still live.
            self.assertEqual(roster, ((ISSUE, REC),))
            # Diagnostic roster: the superseded original is retained.
            self.assertIsNone(diag_err)
            diag_lanes = {(lane, disp) for _, lane, disp, _ in diag}
            self.assertIn((ORIG, "superseded"), diag_lanes)
            self.assertNotIn(REC, {lane for _, lane, _, _ in diag})

    def test_no_lifecycle_rows_keeps_every_live_lane(self) -> None:
        # Byte-invariant: an owner-unbound world (no lifecycle rows) keeps every live
        # lane in the active roster — the pre-#13681 behaviour.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            LaneLifecycleStore(home=home).ensure_schema()  # empty store
            (roster, err), (diag, diag_err) = self._run(home)
            self.assertIsNone(err)
            self.assertEqual(set(roster), {(ISSUE, ORIG), (ISSUE, REC)})
            self.assertEqual(diag, ())

    def test_retired_lane_excluded_from_capacity(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import (
            DISPOSITION_ACTIVE,
            DISPOSITION_RETIRED,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, ORIG), decision=_decision(), issue_id=ISSUE
            )
            store.transition_disposition(
                LaneLifecycleKey(WS, ORIG),
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_RETIRED,
                decision=_decision(),
            )
            (roster, err), (diag, _) = self._run(home)
            self.assertNotIn((ISSUE, ORIG), roster)
            self.assertIn((ORIG, "retired"), {(lane, disp) for _, lane, disp, _ in diag})


class LifecycleDiagnosticScopeTest(unittest.TestCase):
    """R2-F2 (no store creation) + R2-F3 (repo-scoped diagnostic), j#77292."""

    def _hss(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start as hss,
        )

        return hss

    def _seed_superseded(self, store, ws, lane, issue, journal="76630"):
        dec = DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)
        key = LaneLifecycleKey(ws, lane)
        store.declare_active(key, decision=dec, issue_id=issue)
        store.transition_disposition(
            key,
            expected_disposition="active",
            expected_revision=1,
            target="superseded",
            decision=dec,
        )

    def test_diagnostic_read_does_not_create_store(self):
        # R2-F2: an empty home read must not create state.sqlite (read-only contract).
        from mozyo_bridge.core.state.lane_lifecycle import lane_lifecycle_path

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                rows, err = enumerate_lifecycle_diagnostic(Path("."))
            self.assertEqual(rows, ())
            self.assertIsNone(err)
            self.assertFalse(lane_lifecycle_path(home).exists())

    def test_diagnostic_scoped_to_current_repo_workspace(self):
        # R2-F3: only the current repo's workspace rows appear; a foreign repo's
        # superseded lane in the shared home store is never leaked.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            self._seed_superseded(store, "wProj", ORIG, ISSUE)
            self._seed_superseded(store, "wOther", "issue_9999_x", "9999")
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ), patch.object(
                self._hss(), "herdr_workspace_segment", return_value="wProj"
            ):
                diag, err = enumerate_lifecycle_diagnostic(Path("."))
        self.assertIsNone(err)
        self.assertEqual({lane for _, lane, _, _ in diag}, {ORIG})

    def test_unresolved_scope_is_degraded_not_silent_empty(self):
        # R2-F3: when the repo scope cannot be resolved but non-active rows exist, report
        # degraded (fail-closed), never a silent empty that hides them.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            self._seed_superseded(store, WS, ORIG, ISSUE)
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ), patch.object(
                self._hss(),
                "herdr_workspace_segment",
                side_effect=OSError("no anchor"),
            ):
                diag, err = enumerate_lifecycle_diagnostic(Path("."))
        self.assertEqual(diag, ())
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
