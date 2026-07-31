"""Unit tests for the `workflow dispatch-ir` CLI (Redmine #13758 R6-F4 / R7).

Pins the CLI surface of the canonical IR writer: the default dry-run previews the marker-bearing
note WITHOUT any Redmine write, and --execute drives the write -> readback -> EXECUTED handoff
sequence (with injected live seams), failing closed (exit 2) on a non-delivered handoff or a missing
route identity.
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_dispatch_writer import (
    HandoffOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_marker,
)


def _args(**kw):
    base = dict(
        issue="13758", lane="lane-a", generation="1", body="## Gate: Implementation Request",
        body_file=None, target="mzb1_ws1_claude_la", target_repo="/repos/mozyo",
        gateway_callback_target="mzb1_ws1_codex_la",
        role_profile="implementation_worker", source="redmine", to="claude", execute=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class _Patched:
    """Patch the CLI's live seams so --execute never touches a real Redmine / handoff."""

    def __init__(self, *, posted, entries_after_post, delivered=True):
        self.posted = posted
        self._entries_after_post = entries_after_post
        self._delivered = delivered
        self.handoff_calls = []

    def build_live_ir_dispatch(self):
        def post_note(issue, note):
            self.posted.append((issue, note))
            return ""

        def read_entries(issue):
            return self._entries_after_post(self.posted)

        return post_note, read_entries

    def build_live_handoff_send(self, *, issue, route):
        def send(anchor):
            self.handoff_calls.append((anchor, route.target, route.target_repo))
            return HandoffOutcome(delivered=self._delivered)

        return send

    def __enter__(self):
        self._orig = (cli.build_live_ir_dispatch, cli.build_live_handoff_send)
        cli.build_live_ir_dispatch = self.build_live_ir_dispatch
        cli.build_live_handoff_send = self.build_live_handoff_send
        return self

    def __exit__(self, *a):
        cli.build_live_ir_dispatch, cli.build_live_handoff_send = self._orig


class DispatchIrCliTest(unittest.TestCase):
    def test_dry_run_previews_marker_without_writing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_workflow_dispatch_ir(_args())
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn(render_dispatch_marker("lane-a", "1"), printed)
        self.assertIn("dry-run", printed)

    def test_execute_writes_resolves_anchor_and_runs_handoff(self):
        posted = []

        def entries_after_post(posted):
            # pre-read (nothing written yet) -> empty; post-write read -> the marker'd entry.
            if not posted:
                return []
            return [RedmineJournalEntry(issue_id="13758", journal_id="79600", notes=posted[-1][1])]

        with _Patched(posted=posted, entries_after_post=entries_after_post) as p:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_workflow_dispatch_ir(_args(execute=True))
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("dispatch_journal: 79600", printed)
        self.assertIn("handoff_delivered: True", printed)
        self.assertEqual(len(posted), 1)
        self.assertIn(render_dispatch_marker("lane-a", "1"), posted[0][1])
        # the handoff was EXECUTED with the resolved anchor + the route identity.
        self.assertEqual(p.handoff_calls, [("79600", "mzb1_ws1_claude_la", "/repos/mozyo")])

    def test_execute_handoff_not_delivered_fails_closed(self):
        posted = []

        def entries_after_post(posted):
            # pre-read (nothing written yet) -> empty; post-write read -> the marker'd entry.
            if not posted:
                return []
            return [RedmineJournalEntry(issue_id="13758", journal_id="79600", notes=posted[-1][1])]

        with _Patched(posted=posted, entries_after_post=entries_after_post, delivered=False):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.cmd_workflow_dispatch_ir(_args(execute=True))
        self.assertEqual(rc, 2)
        self.assertIn("handoff_failed", err.getvalue())

    def test_execute_missing_target_repo_fails_closed_no_write(self):
        posted = []
        with _Patched(posted=posted, entries_after_post=lambda p: []):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.cmd_workflow_dispatch_ir(_args(execute=True, target_repo=""))
        self.assertEqual(rc, 2)
        self.assertIn("input_invalid", err.getvalue())
        self.assertEqual(posted, [])  # never wrote


class TargetRepoIdentityTest(unittest.TestCase):
    """The route's ``--target-repo`` is a repo identity, not a string the operator happened to type.

    Redmine #14659 (verdict j#92207 / authorization j#92210): the admission gate compares
    ``Path(--target-repo).expanduser().resolve()``, and once ``--target-lane`` is explicit the herdr
    route derives the target ``cwd`` from the same value. A relative ``.`` therefore names whatever
    directory the coordinator's process was in — the same command means different repos from
    different panes. Canonicalize at the CLI boundary; never string-match the gate into agreement.
    """

    def test_a_relative_target_repo_becomes_the_absolute_identity_the_gate_compares(self):
        route = cli._route_from_args(_args(target_repo="."), "lane-a")
        expected = str(Path(".").expanduser().resolve())
        self.assertEqual(route.target_repo, expected)
        self.assertTrue(Path(route.target_repo).is_absolute())

    def test_the_auto_sentinel_is_not_a_path_and_survives(self):
        # `auto` means "infer the root", so resolving it would invent a directory named 'auto'.
        route = cli._route_from_args(_args(target_repo="auto"), "lane-a")
        self.assertEqual(route.target_repo, "auto")

    def test_an_empty_target_repo_stays_empty_so_the_dispatch_still_fails_closed(self):
        # Resolving "" would yield the process cwd and silently satisfy `DispatchRoute.missing()`,
        # turning a fail-closed input error into a dispatch aimed at wherever the caller stood.
        route = cli._route_from_args(_args(target_repo=""), "lane-a")
        self.assertEqual(route.target_repo, "")
        self.assertIn("target_repo", route.missing())

    def test_an_already_absolute_path_is_unchanged_in_identity(self):
        here = str(Path(__file__).resolve().parent)
        route = cli._route_from_args(_args(target_repo=here), "lane-a")
        self.assertEqual(route.target_repo, here)

    def test_the_lane_reaches_the_route_so_the_argv_can_carry_it(self):
        route = cli._route_from_args(_args(), "lane-a")
        self.assertEqual(route.lane, "lane-a")


if __name__ == "__main__":
    unittest.main()
