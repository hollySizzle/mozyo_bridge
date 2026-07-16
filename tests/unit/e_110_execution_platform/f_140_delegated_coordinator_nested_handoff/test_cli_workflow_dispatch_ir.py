"""Unit tests for the `workflow dispatch-ir` CLI (Redmine #13758 R6-F4).

Pins the CLI surface of the canonical IR writer: the default dry-run previews the marker-bearing
note WITHOUT any Redmine write, and --execute drives the write -> readback -> handoff sequence
(with injected live seams), failing closed (exit 2, no handoff) on an unresolved anchor.
"""

from __future__ import annotations

import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    cli_workflow_dispatch_ir as cli,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_marker,
)


def _args(**kw):
    base = dict(
        issue="13758", lane="lane-a", generation="1", body="## Gate: Implementation Request",
        body_file=None, target="mzb1_ws1_claude_la", target_repo="/repos/mozyo",
        role_profile="implementation_worker", source="redmine", to="claude", execute=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class DispatchIrCliTest(unittest.TestCase):
    def test_dry_run_previews_marker_without_writing(self):
        writes = []
        # Guard: the dry-run path must never build the live transport / write.
        self.assertFalse(_args().execute)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_workflow_dispatch_ir(_args())
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn(render_dispatch_marker("lane-a", "1"), printed)
        self.assertIn("dry-run", printed)
        self.assertEqual(writes, [])  # nothing posted

    def test_execute_writes_resolves_anchor_and_emits_handoff(self):
        posted = []

        def fake_build_live():
            def post_note(issue, note):
                posted.append((issue, note))
                return ""

            def read_entries(issue):
                # readback surfaces the just-written marker under a server-assigned journal id.
                return [RedmineJournalEntry(issue_id=str(issue), journal_id="79600", notes=posted[-1][1])]

            return post_note, read_entries

        orig = cli.build_live_ir_dispatch
        cli.build_live_ir_dispatch = fake_build_live
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_workflow_dispatch_ir(_args(execute=True))
        finally:
            cli.build_live_ir_dispatch = orig
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("dispatch_journal: 79600", printed)
        self.assertIn("--journal 79600", printed)
        self.assertEqual(len(posted), 1)
        self.assertIn(render_dispatch_marker("lane-a", "1"), posted[0][1])

    def test_execute_unresolved_anchor_fails_closed(self):
        def fake_build_live():
            def post_note(issue, note):
                return ""

            def read_entries(issue):
                # readback has no marker (legacy prose) -> unresolved -> no handoff.
                return [RedmineJournalEntry(issue_id=str(issue), journal_id="79600", notes="prose only")]

            return post_note, read_entries

        orig = cli.build_live_ir_dispatch
        cli.build_live_ir_dispatch = fake_build_live
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.cmd_workflow_dispatch_ir(_args(execute=True))
        finally:
            cli.build_live_ir_dispatch = orig
        self.assertEqual(rc, 2)
        self.assertNotIn("--journal", out.getvalue())
        self.assertIn("anchor_unresolved", err.getvalue())


if __name__ == "__main__":
    unittest.main()
