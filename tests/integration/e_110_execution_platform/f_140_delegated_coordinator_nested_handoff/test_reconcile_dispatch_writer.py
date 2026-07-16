"""Production-path tests for the canonical IR dispatch writer (Redmine #13758 R6-F4 / j#79507 Q2).

Drives the real write -> readback -> handoff sequence of
:func:`...application.reconcile_dispatch_writer.dispatch_implementation_request` with injected
Redmine seams (a capturing ``post_note`` + a production-shape journals ``read_entries``) — the
production path the review required, NOT a ``render_dispatch_note`` helper unit test:

- the written IR note carries the machine dispatch marker;
- the anchor is resolved from the readback's OWNING entry journal id (server-assigned), never a
  self-reported field, and only that anchor drives the gated handoff (``--journal <anchor>``);
- a write failure / readback failure / unresolved / ambiguous marker each fails closed: no handoff.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_dispatch_writer import (
    DISPATCH_ANCHOR_UNRESOLVED,
    DISPATCH_READBACK_FAILED,
    DISPATCH_WRITE_FAILED,
    DISPATCH_WRITTEN,
    build_ir_handoff_command,
    dispatch_implementation_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_marker,
    resolve_dispatch_entry_journal,
)


class _FakeRedmine:
    """A minimal Redmine double: ``post_note`` records the note as a server-assigned journal entry.

    Models the real contract: ``PUT /issues/<id>.json`` creates a journal entry (Redmine returns 204
    with no id — so ``post_note`` returns ""), and a later readback GET returns that entry with the
    server-assigned ``journal_id``. ``resolve_dispatch_entry_journal`` then reads the marker from the
    readback entry and returns the entry's OWN id — exactly the production readback anchor.
    """

    def __init__(self, *, assigned_journal_id="79600", preexisting=()):
        self.assigned_journal_id = assigned_journal_id
        self.entries = list(preexisting)
        self.posted = []

    def post_note(self, issue, note):
        self.posted.append((issue, note))
        # The server assigns the journal id; the readback surfaces it as the OWNING entry id.
        self.entries.append(
            RedmineJournalEntry(issue_id=str(issue), journal_id=self.assigned_journal_id, notes=note)
        )
        return ""  # Redmine 204: no journal id in the write response

    def read_entries(self, issue):
        return list(self.entries)


def _handoff_builder(anchor):
    return build_ir_handoff_command(
        issue="13758", target="mzb1_ws1_claude_la", target_repo="/repos/mozyo",
        dispatch_journal=anchor,
    )


class DispatchWriteReadbackHandoffTest(unittest.TestCase):
    def test_marker_written_anchor_from_readback_drives_handoff(self):
        redmine = _FakeRedmine(assigned_journal_id="79600")
        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1,
            body="## Gate: Implementation Request\nevent-driven reconcile",
            post_note=redmine.post_note, read_entries=redmine.read_entries,
            handoff_builder=_handoff_builder,
        )
        self.assertEqual(result.status, DISPATCH_WRITTEN)
        self.assertTrue(result.sendable)
        # (1) the written note embeds the machine dispatch marker.
        self.assertEqual(len(redmine.posted), 1)
        posted_note = redmine.posted[0][1]
        self.assertIn(render_dispatch_marker("lane-a", 1), posted_note)
        # (2) the anchor is the readback entry's OWN (server-assigned) journal id, not self-reported.
        self.assertEqual(result.dispatch_journal, "79600")
        # cross-check: the reader resolves the same owning-entry anchor from the readback.
        self.assertEqual(
            resolve_dispatch_entry_journal(redmine.read_entries("13758"), lane="lane-a", lane_generation=1),
            "79600",
        )
        # (3) the gated handoff command is anchored on that journal id.
        self.assertIn("--journal 79600", result.handoff_command)
        self.assertIn("--kind implementation_request", result.handoff_command)

    def test_write_failure_fails_closed_no_handoff(self):
        def boom_post(issue, note):
            raise RuntimeError("redmine down")

        redmine = _FakeRedmine()
        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1, body="body",
            post_note=boom_post, read_entries=redmine.read_entries, handoff_builder=_handoff_builder,
        )
        self.assertEqual(result.status, DISPATCH_WRITE_FAILED)
        self.assertFalse(result.sendable)
        self.assertEqual(result.dispatch_journal, "")
        self.assertEqual(result.handoff_command, "")

    def test_readback_failure_fails_closed_no_handoff(self):
        redmine = _FakeRedmine()

        def boom_read(issue):
            raise RuntimeError("readback transport error")

        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1, body="body",
            post_note=redmine.post_note, read_entries=boom_read, handoff_builder=_handoff_builder,
        )
        # the write happened (durable intent persists), but no anchor -> no handoff.
        self.assertEqual(result.status, DISPATCH_READBACK_FAILED)
        self.assertFalse(result.sendable)
        self.assertEqual(result.handoff_command, "")
        self.assertEqual(len(redmine.posted), 1)

    def test_legacy_prose_only_readback_is_unresolved_no_handoff(self):
        # The readback returns the note WITHOUT the marker (e.g. a stripping proxy) -> never guessed.
        redmine = _FakeRedmine()

        def strip_marker_read(issue):
            return [RedmineJournalEntry(issue_id="13758", journal_id="79600",
                                        notes="## Gate: Implementation Request — prose only")]

        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1, body="body",
            post_note=redmine.post_note, read_entries=strip_marker_read, handoff_builder=_handoff_builder,
        )
        self.assertEqual(result.status, DISPATCH_ANCHOR_UNRESOLVED)
        self.assertFalse(result.sendable)
        self.assertEqual(result.handoff_command, "")

    def test_ambiguous_two_distinct_dispatches_is_unresolved_no_handoff(self):
        # A foreign / duplicate structured dispatch for the same generation -> zero-send.
        note = f"body\n\n{render_dispatch_marker('lane-a', 1)}"
        preexisting = [RedmineJournalEntry(issue_id="13758", journal_id="79111", notes=note)]
        redmine = _FakeRedmine(assigned_journal_id="79600", preexisting=preexisting)
        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1, body="body",
            post_note=redmine.post_note, read_entries=redmine.read_entries, handoff_builder=_handoff_builder,
        )
        self.assertEqual(result.status, DISPATCH_ANCHOR_UNRESOLVED)
        self.assertFalse(result.sendable)
        self.assertEqual(result.handoff_command, "")


if __name__ == "__main__":
    unittest.main()
