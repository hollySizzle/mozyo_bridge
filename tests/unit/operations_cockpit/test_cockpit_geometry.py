"""Cockpit geometry diagnosis — read-only drift detection (Redmine #12131).

`mozyo cockpit doctor-geometry` diagnoses live cockpit display-geometry drift
without mutating tmux. These tests pin the pure diagnoser
(:func:`diagnose_cockpit_geometry`) — column clustering, Unit grouping, and every
finding category — plus the text formatter and the read-only CLI wiring, all
hermetic (no live tmux). The load-bearing case is the #12130 manual-recovery
drift: a half-bound role-less pane leaves its workspace Unit missing a claude,
and both are reported as observed geometry, never as identity authority.
Synthetic, neutral identifiers only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_geometry import (
    FINDING_DUPLICATE_ROLE,
    FINDING_MISSING_CLAUDE,
    FINDING_MISSING_CODEX,
    FINDING_MIXED_UNIT_COLUMN,
    FINDING_NARROW_PANE,
    FINDING_ROLE_LESS_PANE,
    FINDING_UNIT_COLUMN_SPLIT,
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    diagnose_cockpit_geometry,
    format_geometry_text,
)


def _pane(
    pane_id,
    *,
    workspace_id="",
    role="",
    lane_id="default",
    left=0,
    top=0,
    width=80,
    height=40,
):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": left,
        "pane_top": top,
        "pane_width": width,
        "pane_height": height,
    }


def _healthy_unit(prefix, *, workspace_id, left, width):
    """A well-formed column: codex on top, claude on bottom, same x-range."""
    return [
        _pane(
            f"%{prefix}c",
            workspace_id=workspace_id,
            role="codex",
            left=left,
            top=0,
            width=width,
            height=39,
        ),
        _pane(
            f"%{prefix}l",
            workspace_id=workspace_id,
            role="claude",
            left=left,
            top=39,
            width=width,
            height=17,
        ),
    ]


def _codes(diagnosis):
    return [f.code for f in diagnosis.findings]


class HealthyCockpitTest(unittest.TestCase):
    def test_two_clean_columns_have_no_findings(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        panes += _healthy_unit("1", workspace_id="wsB", left=40, width=40)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertTrue(diag.cockpit_present)
        self.assertTrue(diag.ok)
        self.assertEqual([], list(diag.findings))
        self.assertEqual(2, len(diag.columns))
        self.assertEqual(2, len(diag.units))
        # Each Unit's codex+claude landed in one shared column.
        for unit in diag.units:
            self.assertEqual(1, len(unit.columns))
            self.assertTrue(unit.has_codex)
            self.assertTrue(unit.has_claude)

    def test_stacked_pair_shares_one_column(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertEqual(1, len(diag.columns))
        self.assertEqual(("%0c", "%0l"), diag.columns[0].pane_ids)


class AbsentCockpitTest(unittest.TestCase):
    def test_none_panes_is_benign_no_op(self) -> None:
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=None)
        self.assertFalse(diag.cockpit_present)
        self.assertTrue(diag.ok)
        self.assertEqual((), diag.panes)
        self.assertEqual((), diag.findings)
        text = format_geometry_text(diag)
        self.assertIn("nothing to diagnose", text)


class DriftSampleTest(unittest.TestCase):
    """The #12130 manual-recovery drift: one role-less, half-bound pane."""

    def _panes(self):
        return [
            _pane("%1104", workspace_id="video", role="codex", left=0, top=0, width=41, height=39),
            _pane("%953", workspace_id="bridge", role="codex", left=41, top=0, width=32, height=39),
            # %1106 was created by hand and never got its role/workspace markers.
            _pane("%1106", workspace_id="", role="", left=0, top=39, width=41, height=17),
            _pane("%954", workspace_id="bridge", role="claude", left=41, top=39, width=32, height=17),
        ]

    def test_reports_missing_claude_and_role_less(self) -> None:
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=self._panes())
        self.assertFalse(diag.ok)
        codes = _codes(diag)
        self.assertIn(FINDING_MISSING_CLAUDE, codes)
        self.assertIn(FINDING_ROLE_LESS_PANE, codes)
        # The healthy `bridge` Unit is not falsely flagged.
        self.assertNotIn(FINDING_MISSING_CODEX, codes)
        self.assertNotIn(FINDING_MIXED_UNIT_COLUMN, codes)
        self.assertNotIn(FINDING_UNIT_COLUMN_SPLIT, codes)

    def test_missing_claude_points_at_the_video_unit(self) -> None:
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=self._panes())
        missing = next(f for f in diag.findings if f.code == FINDING_MISSING_CLAUDE)
        self.assertEqual("video", missing.workspace_id)
        self.assertEqual(("%1104",), missing.pane_ids)
        self.assertEqual(SEVERITY_WARNING, missing.severity)

    def test_role_less_finding_names_the_pane_and_missing_markers(self) -> None:
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=self._panes())
        roleless = next(f for f in diag.findings if f.code == FINDING_ROLE_LESS_PANE)
        self.assertEqual(("%1106",), roleless.pane_ids)
        self.assertIn("@mozyo_workspace_id", roleless.message)
        self.assertIn("@mozyo_agent_role", roleless.message)
        self.assertIn("not identity authority", roleless.message)


class MixedUnitColumnTest(unittest.TestCase):
    def test_two_units_in_one_x_range_is_mixed(self) -> None:
        panes = [
            _pane("%a", workspace_id="wsA", role="codex", left=0, top=0, width=40, height=56),
            _pane("%b", workspace_id="wsB", role="codex", left=0, top=0, width=40, height=56),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertFalse(diag.ok)
        finding = next(f for f in diag.findings if f.code == FINDING_MIXED_UNIT_COLUMN)
        self.assertEqual(SEVERITY_WARNING, finding.severity)
        self.assertEqual({"%a", "%b"}, set(finding.pane_ids))


class UnitColumnSplitTest(unittest.TestCase):
    def test_unit_codex_claude_in_different_columns(self) -> None:
        panes = [
            _pane("%c", workspace_id="wsA", role="codex", left=0, top=0, width=40, height=56),
            _pane("%l", workspace_id="wsA", role="claude", left=50, top=0, width=40, height=56),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertFalse(diag.ok)
        codes = _codes(diag)
        self.assertIn(FINDING_UNIT_COLUMN_SPLIT, codes)
        # Same Unit in two columns is a split, NOT a mixed-Unit column.
        self.assertNotIn(FINDING_MIXED_UNIT_COLUMN, codes)


class WidthImbalanceTest(unittest.TestCase):
    def test_narrow_column_is_a_notice_not_a_failure(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        panes += _healthy_unit("1", workspace_id="wsB", left=40, width=40)
        panes += _healthy_unit("2", workspace_id="wsC", left=80, width=8)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        codes = _codes(diag)
        self.assertIn(FINDING_NARROW_PANE, codes)
        narrow = next(f for f in diag.findings if f.code == FINDING_NARROW_PANE)
        self.assertEqual(SEVERITY_NOTICE, narrow.severity)
        # A notice alone leaves the cockpit `ok` (drift-free of warnings).
        self.assertTrue(diag.ok)

    def test_single_column_is_never_width_flagged(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=8)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertNotIn(FINDING_NARROW_PANE, _codes(diag))


class DuplicateRoleTest(unittest.TestCase):
    """A Unit must hold exactly one codex + one claude; duplicates are drift.

    `cockpit reconcile` (#12136) fail-closes on a duplicate role per Unit, but the
    read-only `doctor-geometry` did not surface it proactively until #12310. Two
    codex panes stamped for one workspace/lane is observed geometry drift, not an
    identity re-decision.
    """

    def test_two_codex_in_one_unit_is_a_warning(self) -> None:
        panes = [
            _pane("%c1", workspace_id="wsA", role="codex", left=0, top=0, width=40, height=28),
            _pane("%c2", workspace_id="wsA", role="codex", left=0, top=28, width=40, height=28),
            _pane("%l", workspace_id="wsA", role="claude", left=40, top=0, width=40, height=56),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertFalse(diag.ok)
        finding = next(f for f in diag.findings if f.code == FINDING_DUPLICATE_ROLE)
        self.assertEqual(SEVERITY_WARNING, finding.severity)
        self.assertEqual("wsA", finding.workspace_id)
        self.assertEqual({"%c1", "%c2"}, set(finding.pane_ids))
        self.assertIn("codex", finding.message)
        self.assertIn("not identity authority", finding.message)
        # The peer claude is present, so this is not also a missing-role finding.
        self.assertNotIn(FINDING_MISSING_CLAUDE, _codes(diag))
        self.assertNotIn(FINDING_MISSING_CODEX, _codes(diag))

    def test_two_claude_in_one_unit_is_a_warning(self) -> None:
        panes = [
            _pane("%c", workspace_id="wsA", role="codex", left=0, top=0, width=40, height=56),
            _pane("%l1", workspace_id="wsA", role="claude", left=40, top=0, width=40, height=28),
            _pane("%l2", workspace_id="wsA", role="claude", left=40, top=28, width=40, height=28),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        finding = next(f for f in diag.findings if f.code == FINDING_DUPLICATE_ROLE)
        self.assertEqual({"%l1", "%l2"}, set(finding.pane_ids))
        self.assertIn("claude", finding.message)

    def test_healthy_unit_has_no_duplicate_role_finding(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertNotIn(FINDING_DUPLICATE_ROLE, _codes(diag))

    def test_distinct_lanes_under_one_workspace_are_not_duplicates(self) -> None:
        # Two codex panes that belong to *different* lanes are two Units, not a
        # duplicate role within one Unit.
        panes = [
            _pane("%c1", workspace_id="wsA", role="codex", lane_id="default", left=0, top=0, width=40, height=56),
            _pane("%c2", workspace_id="wsA", role="codex", lane_id="featureX", left=40, top=0, width=40, height=56),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        self.assertNotIn(FINDING_DUPLICATE_ROLE, _codes(diag))


class MissingCodexTest(unittest.TestCase):
    def test_claude_only_unit_reports_missing_codex(self) -> None:
        panes = [
            _pane("%l", workspace_id="wsA", role="claude", left=0, top=0, width=40, height=56),
        ]
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        codes = _codes(diag)
        self.assertIn(FINDING_MISSING_CODEX, codes)
        self.assertNotIn(FINDING_MISSING_CLAUDE, codes)


class SerializationTest(unittest.TestCase):
    def test_as_dict_is_json_round_trippable(self) -> None:
        panes = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        diag = diagnose_cockpit_geometry(session="mozyo-cockpit", panes=panes)
        payload = json.loads(json.dumps(diag.as_dict()))
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["pane_count"])
        self.assertEqual("mozyo-cockpit", payload["session"])
        self.assertEqual({"warning": 0, "notice": 0}, payload["summary"])


class CliWiringTest(unittest.TestCase):
    """`mozyo cockpit doctor-geometry` is read-only and mirrors doctor's exit."""

    def _args(self, json_output=False):
        return argparse.Namespace(
            action="doctor-geometry",
            cockpit_session=None,
            json_output=json_output,
            dry_run=False,
        )

    def test_drift_exits_nonzero_but_emits_json(self) -> None:
        from mozyo_bridge.application import commands

        drift = [
            _pane("%1104", workspace_id="video", role="codex", left=0, top=0, width=41, height=39),
            _pane("%1106", workspace_id="", role="", left=0, top=39, width=41, height=17),
        ]
        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=drift):
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_cockpit(self._args(json_output=True))
        self.assertEqual(1, rc)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn(FINDING_MISSING_CLAUDE, [f["code"] for f in payload["findings"]])

    def test_clean_cockpit_exits_zero(self) -> None:
        from mozyo_bridge.application import commands

        clean = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        clean += _healthy_unit("1", workspace_id="wsB", left=40, width=40)
        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=clean):
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_cockpit(self._args(json_output=False))
        self.assertEqual(0, rc)
        self.assertIn("no cockpit geometry drift", buf.getvalue())

    def test_absent_cockpit_exits_zero(self) -> None:
        from mozyo_bridge.application import commands

        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=None):
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_cockpit(self._args(json_output=False))
        self.assertEqual(0, rc)
        self.assertIn("nothing to diagnose", buf.getvalue())

    def test_doctor_geometry_never_requires_tmux_mutation(self) -> None:
        # The read-only path must not invoke `require_tmux` (which would gate on a
        # mutable server) — it short-circuits before that, like adopt/reset preview.
        from mozyo_bridge.application import commands

        clean = _healthy_unit("0", workspace_id="wsA", left=0, width=40)
        with patch.object(commands, "_read_cockpit_geometry", return_value=clean):
            with patch.object(commands, "require_tmux") as req:
                with contextlib.redirect_stdout(io.StringIO()):
                    commands.cmd_cockpit(self._args(json_output=True))
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
