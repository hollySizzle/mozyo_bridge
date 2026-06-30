"""`workflow dispatch-plan` CLI integration tests (Redmine #12920).

Covers the Version-bucket lane-set dispatch plan command surface:

- the subcommand registers under the ``workflow`` family;
- a bucket snapshot enumerates open leaf candidates and skips closed / non-leaf issues;
- a candidate with a file overlap against an active lane is ``standby``; a release-gate
  candidate is ``needs_owner_decision``;
- the coordinator-owned queue (review / owner / integration waiting) is projected from
  ``--lane-signal`` lanes;
- ``--json`` emits one structured envelope and ``--journal`` emits the markdown;
- a closed/missing Version yields an unresolved plan (still exit 0);
- ``--bucket-id`` / ``--issues-json`` are required and a missing snapshot fails closed;
- the command never mutates: a dry-run / execute run reads only the supplied files.
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
    cli_workflow_dispatch_plan,
)

_ISSUES = {
    "issues": [
        {
            "id": 1,
            "tracker": {"name": "開発"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292, "name": "枠", "status": "open"},
            "parent": {"id": 99},
        },
        {
            "id": 2,
            "tracker": {"name": "Task"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292},
            "parent": {"id": 99},
        },
        {
            "id": 99,
            "tracker": {"name": "User Story"},
            "status": {"name": "New", "is_closed": False},
            "fixed_version": {"id": 292},
        },
        {
            "id": 3,
            "tracker": {"name": "Task"},
            "status": {"name": "Closed", "is_closed": True},
            "fixed_version": {"id": 292},
        },
    ]
}


def _write(tmp: Path, name: str, payload) -> str:
    path = tmp / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class RegistrationTest(unittest.TestCase):
    def test_dispatch_plan_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", "x.json"]
        )
        self.assertIs(
            ns.func, cli_workflow_dispatch_plan.cmd_workflow_dispatch_plan
        )


class PlanTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.issues = _write(self.tmp, "issues.json", _ISSUES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_enumerates_and_classifies(self):
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-id", "292", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)
        self.assertIn("candidate: 1", out)
        self.assertIn("candidate: 2", out)
        # closed + non-leaf are skipped, not dispatched.
        self.assertIn("skipped: 99 -> not_leaf", out)
        self.assertIn("skipped: 3 -> issue_closed", out)

    def test_file_overlap_is_standby(self):
        facts = _write(self.tmp, "facts.json", {"2": {"file_overlap": ["500"]}})
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--candidate-facts",
                facts,
                "--lane-signal",
                "500:start",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("candidate: 2", out)
        self.assertIn("standby", out)

    def test_queue_projection(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--lane-signal",
                "500:review_request",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("review_waiting=['500']", out)

    def test_json_envelope(self):
        facts = _write(self.tmp, "facts.json", {"1": {"release_publish_gate": True}})
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--candidate-facts",
                facts,
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["bucket_id"], "292")
        self.assertTrue(payload["resolved"])
        by_id = {c["issue_id"]: c for c in payload["candidates"]}
        self.assertEqual(by_id["1"]["classification"], "needs_owner_decision")
        self.assertEqual(
            payload["recommended_route"],
            "coordinator_codex -> sublane_codex_gateway -> same_lane_claude",
        )

    def test_journal_markdown(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--journal",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("## Lane-set dispatch plan", out)
        self.assertIn("counts_by_classification:", out)

    def test_resolve_by_name(self):
        # Version name resolves from the issues' embedded fixed_version name ("枠"),
        # even without a --versions-json snapshot (acceptance: id/name selector).
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-name", "枠", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: true", out)
        self.assertIn("bucket_id: 292", out)
        self.assertIn("candidate: 1", out)

    def test_unknown_name_is_unresolved(self):
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-name",
                "存在しない枠",
                "--issues-json",
                self.issues,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: bucket_not_found", out)

    def test_ambiguous_name_fails_closed(self):
        versions = _write(
            self.tmp,
            "versions.json",
            {"versions": [{"id": 292, "name": "重複枠"}, {"id": 293, "name": "重複枠"}]},
        )
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-name",
                "重複枠",
                "--issues-json",
                self.issues,
                "--versions-json",
                versions,
            ]
        )
        self.assertEqual(rc, 0)
        # An ambiguous name is never guessed -> unresolved, ambiguous_source skip.
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: ambiguous_source", out)
        self.assertIn("292", out)
        self.assertIn("293", out)

    def test_missing_bucket_is_unresolved(self):
        rc, out = _run(
            ["workflow", "dispatch-plan", "--bucket-id", "999", "--issues-json", self.issues]
        )
        self.assertEqual(rc, 0)
        self.assertIn("resolved: false", out)
        self.assertIn("bucket_skip: bucket_not_found", out)

    def test_execute_mode_is_read_only(self):
        # execute records intent only; the plan is identical and nothing is mutated.
        rc, out = _run(
            [
                "workflow",
                "dispatch-plan",
                "--bucket-id",
                "292",
                "--issues-json",
                self.issues,
                "--mode",
                "execute",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("mode: execute", out)
        self.assertIn("candidate: 1", out)

    def test_missing_snapshot_fails_closed(self):
        with self.assertRaises(SystemExit):
            _run(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--issues-json",
                    str(self.tmp / "nope.json"),
                ]
            )


class ParsingTest(unittest.TestCase):
    def test_a_bucket_selector_is_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "dispatch-plan", "--issues-json", "x.json"])

    def test_bucket_id_and_name_are_mutually_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "workflow",
                    "dispatch-plan",
                    "--issues-json",
                    "x.json",
                    "--bucket-id",
                    "292",
                    "--bucket-name",
                    "枠",
                ]
            )

    def test_issues_json_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "dispatch-plan", "--bucket-id", "292"])

    def test_malformed_lane_signal_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "workflow",
                    "dispatch-plan",
                    "--bucket-id",
                    "292",
                    "--issues-json",
                    "x.json",
                    "--lane-signal",
                    "noselector",
                ]
            )


if __name__ == "__main__":
    unittest.main()
