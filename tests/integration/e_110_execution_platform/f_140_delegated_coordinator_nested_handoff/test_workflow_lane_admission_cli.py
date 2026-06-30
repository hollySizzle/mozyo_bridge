"""`workflow lane-admission` CLI integration tests (Redmine #12921).

Covers the risk-based per-candidate admission command surface:

- the subcommand registers under the ``workflow`` family;
- a candidate with no risk reports ``allow_dispatch`` and returns 0;
- coordinator-convenience flags alone still report ``allow_dispatch`` with the flag
  named under ``rejected_nonreasons`` (the owner correction, end to end);
- a file-overlap candidate reports ``serialize``; a dependency on a ``blocked`` lane
  reports ``blocked``; a release gate reports ``needs_owner_decision`` (all exit 0:
  advisory);
- ``--json`` emits one structured envelope and ``--journal`` emits the markdown;
- ``--candidate`` is required and ``--lane-signal`` rejects a malformed spec.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_lane_admission,
)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_lane_admission_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "lane-admission", "--candidate", "12921"])
        self.assertIs(ns.func, cli_workflow_lane_admission.cmd_workflow_lane_admission)


class DecisionTest(unittest.TestCase):
    def test_no_risk_allows_dispatch(self):
        rc, out = _run(["workflow", "lane-admission", "--candidate", "12921"])
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: allow_dispatch", out)

    def test_callback_miss_concern_alone_allows_dispatch(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--callback-miss-concern",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: allow_dispatch", out)
        self.assertIn("callback_miss_risk", out)

    def test_file_overlap_serializes(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--file-overlap",
                "12639",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: serialize", out)
        self.assertIn("file_overlap", out)

    def test_dependency_on_blocked_lane_blocks(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--lane-signal",
                "12500:blocked",
                "--dependency",
                "12500",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: blocked", out)

    def test_release_gate_needs_owner_decision(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--release-publish-gate",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("admission_decision: needs_owner_decision", out)

    def test_json_envelope(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--file-overlap",
                "12639",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["candidate_issue"], "12921")
        self.assertEqual(payload["decision"], "serialize")
        self.assertEqual(payload["risks"][0]["reason"], "file_overlap")

    def test_journal_markdown(self):
        rc, out = _run(
            [
                "workflow",
                "lane-admission",
                "--candidate",
                "12921",
                "--file-overlap",
                "12639",
                "--journal",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("## Lane admission decision", out)
        self.assertIn("admission_decision: serialize", out)


class ParsingTest(unittest.TestCase):
    def test_candidate_is_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "lane-admission"])

    def test_malformed_lane_signal_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["workflow", "lane-admission", "--candidate", "12921", "--lane-signal", "noselector"]
            )


if __name__ == "__main__":
    unittest.main()
