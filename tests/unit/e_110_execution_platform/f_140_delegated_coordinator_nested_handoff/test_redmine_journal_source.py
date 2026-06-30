"""Redmine journal read-boundary tests (Redmine #12672 review j#68992 fix).

Pins the boundary that reads Redmine issue/journal history and extracts the structured gate
markers in it into :class:`JournalMarker` inputs, so ``workflow watch`` ingests real Redmine
history rather than only hand-typed ``--marker`` strings:

- a gate is read from the machine ``[mozyo:<channel>:...]`` marker token, never from prose: a
  note without a recognized marker yields nothing, even when its prose mentions "review";
- only gate-bearing kinds (implementation_done / review_request / review_result) become a
  marker; a non-gate kind (implementation_request / design_consultation) is skipped;
- each journal entry is keyed by its own redmine:<issue>:<journal_id> anchor;
- the workflow-event channel carries the conclusion (review_result -> review approved);
- the MappingRedmineJournalSource reads the issues.json / get_issue_detail payload shape and
  drops field-only (empty-note) journals.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    RedmineJournalEntry,
    extract_marker,
    extract_markers,
    extract_markers_from_note,
    markers_from_source,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_REVIEW,
)


def _handoff_marker(issue, journal, kind, to="codex"):
    return f"[mozyo:handoff:source=redmine:issue={issue}:journal={journal}:kind={kind}:to={to}]"


class ExtractFromNoteTest(unittest.TestCase):
    def test_handoff_marker_review_request_extracted(self):
        note = "## Implementation Done / Review Request\n" + _handoff_marker(
            "12672", "68989", "review_request"
        )
        markers = extract_markers_from_note("12672", "68989", note)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].gate, "review_request")
        self.assertEqual(markers[0].event_id, "redmine:12672:68989")

    def test_prose_without_marker_yields_nothing(self):
        # The note talks about a review in prose but carries no structured marker token.
        note = "## Gate: review\nThe reviewer approved the change after reading the diff."
        self.assertEqual(extract_markers_from_note("12672", "1", note), ())

    def test_non_gate_kind_is_skipped(self):
        # implementation_request is a dispatch, not a completion gate.
        note = _handoff_marker("12672", "1", "implementation_request")
        self.assertEqual(extract_markers_from_note("12672", "1", note), ())

    def test_workflow_event_channel_carries_conclusion(self):
        note = "[mozyo:workflow-event:gate=review_result:conclusion=approved]"
        markers = extract_markers_from_note("12672", "69100", note)
        self.assertEqual(len(markers), 1)
        # review_result aliases to the runtime review gate with the carried conclusion.
        self.assertEqual(markers[0].gate, GATE_REVIEW)
        self.assertEqual(markers[0].review_conclusion, "approved")

    def test_anchor_is_the_entry_journal_not_the_marker_field(self):
        # The marker names journal=999 internally, but the entry's own id is the anchor.
        note = _handoff_marker("12672", "999", "review_request")
        markers = extract_markers_from_note("12672", "68989", note)
        self.assertEqual(markers[0].event_id, "redmine:12672:68989")

    def test_unknown_channel_ignored(self):
        note = "[mozyo:unknownchannel:gate=review_request]"
        self.assertEqual(extract_markers_from_note("12672", "1", note), ())

    def test_malformed_conclusion_fails_closed_skipped(self):
        note = "[mozyo:workflow-event:gate=review_result:conclusion=maybe]"
        self.assertEqual(extract_markers_from_note("12672", "1", note), ())

    def test_multiple_markers_in_one_note(self):
        note = (
            _handoff_marker("12672", "68989", "implementation_done")
            + "\n"
            + _handoff_marker("12672", "68989", "review_request")
        )
        markers = extract_markers_from_note("12672", "68989", note)
        self.assertEqual([m.gate for m in markers], ["implementation_done", "review_request"])


class ExtractMarkersTest(unittest.TestCase):
    def test_extract_marker_first_only(self):
        entry = RedmineJournalEntry(
            "12672", "68989", _handoff_marker("12672", "68989", "review_request")
        )
        self.assertEqual(extract_marker(entry).gate, "review_request")

    def test_extract_marker_none_when_no_marker(self):
        entry = RedmineJournalEntry("12672", "1", "just prose")
        self.assertIsNone(extract_marker(entry))

    def test_extract_markers_in_order(self):
        entries = [
            RedmineJournalEntry("12672", "1", "prose only"),
            RedmineJournalEntry("12672", "68989", _handoff_marker("12672", "68989", "implementation_done")),
            RedmineJournalEntry("12672", "69100", "[mozyo:workflow-event:gate=review_request]"),
        ]
        markers = extract_markers(entries)
        self.assertEqual(
            [(m.issue, m.journal, m.gate) for m in markers],
            [("12672", "68989", "implementation_done"), ("12672", "69100", "review_request")],
        )


class MappingSourceTest(unittest.TestCase):
    def _payload(self):
        return {
            "issue": {"id": "12672"},
            "journals": [
                {"id": "68978", "notes": "## Start\nno marker here"},
                {"id": "68989", "notes": _handoff_marker("12672", "68989", "review_request")},
                {"id": "69050", "notes": ""},  # field-only journal: dropped
                {"id": "69100", "notes": "[mozyo:workflow-event:gate=review_result:conclusion=changes_requested]"},
            ],
        }

    def test_reads_entries_dropping_empty_notes(self):
        source = MappingRedmineJournalSource(payload=self._payload())
        entries = source.read_entries()
        # 68978 (prose), 68989 (marker), 69100 (marker) — 69050 empty-note dropped.
        self.assertEqual([e.journal_id for e in entries], ["68978", "68989", "69100"])

    def test_markers_from_source_extracts_only_marked_gates(self):
        source = MappingRedmineJournalSource(payload=self._payload())
        markers = markers_from_source(source, "12672")
        self.assertEqual(
            [(m.journal, m.gate) for m in markers],
            [("68989", "review_request"), ("69100", GATE_REVIEW)],
        )

    def test_issue_id_from_payload_when_arg_absent(self):
        source = MappingRedmineJournalSource(payload=self._payload())
        markers = markers_from_source(source, "")
        self.assertTrue(all(m.issue == "12672" for m in markers))

    def test_bare_journals_payload_with_explicit_issue(self):
        payload = {"journals": [{"id": "5", "notes": _handoff_marker("12672", "5", "review_request")}]}
        source = MappingRedmineJournalSource(payload=payload)
        markers = markers_from_source(source, "12672")
        self.assertEqual(markers[0].event_id, "redmine:12672:5")


if __name__ == "__main__":
    unittest.main()
