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
    render_workflow_event_marker,
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

    def test_malformed_conclusion_is_recognized_as_non_explicit(self):
        # Redmine #13974 j#81512: a RECOGNIZED review_result gate with an out-of-vocabulary conclusion
        # is NOT dropped (that would let a newer malformed result be invisible so an older valid result
        # stays "latest" and delivers). It stays recognized with a non-explicit (pending) conclusion so
        # it shadows the old result; the callback fence then refuses the non-explicit conclusion.
        note = "[mozyo:workflow-event:gate=review_result:conclusion=maybe]"
        markers = extract_markers_from_note("12672", "1", note)
        self.assertEqual([m.gate for m in markers], ["review"])
        self.assertEqual(markers[0].review_conclusion, "pending")

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


class NestedRestShapeTest(unittest.TestCase):
    """The Redmine REST shape nests journals under issue.journals (review j#69006)."""

    def test_nested_issue_journals_are_read(self):
        # The /issues/<id>.json?include=journals shape: journals under the issue.
        payload = {
            "issue": {
                "id": "12672",
                "journals": [
                    {"id": "68989", "notes": _handoff_marker("12672", "68989", "review_request")},
                ],
            }
        }
        source = MappingRedmineJournalSource(payload=payload)
        markers = markers_from_source(source, "")
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].event_id, "redmine:12672:68989")
        # The issue id resolves from issue.id too.
        self.assertEqual(markers[0].issue, "12672")

    def test_top_level_journals_take_precedence(self):
        # When both are present the top-level (MCP wrapper) list wins; the nested one is a
        # stale duplicate in that wrapper shape.
        payload = {
            "issue": {
                "id": "12672",
                "journals": [{"id": "999", "notes": _handoff_marker("12672", "999", "review_request")}],
            },
            "journals": [{"id": "68989", "notes": _handoff_marker("12672", "68989", "review_request")}],
        }
        source = MappingRedmineJournalSource(payload=payload)
        entries = source.read_entries()
        self.assertEqual([e.journal_id for e in entries], ["68989"])

    def test_empty_nested_journal_list_yields_nothing_not_crash(self):
        payload = {"issue": {"id": "12672", "journals": []}}
        source = MappingRedmineJournalSource(payload=payload)
        self.assertEqual(source.read_entries(), [])
        self.assertEqual(markers_from_source(source, ""), ())

    def test_no_journals_anywhere_yields_empty(self):
        payload = {"issue": {"id": "12672"}}
        source = MappingRedmineJournalSource(payload=payload)
        self.assertEqual(source.read_entries(), [])


class RenderWorkflowEventMarkerTest(unittest.TestCase):
    """The gate-journal marker PRODUCER (#13520 review F1-R1): render round-trips to a marker."""

    def test_bare_marker_round_trips_through_the_classifier(self):
        token = render_workflow_event_marker("review_request")
        self.assertEqual(token, "[mozyo:workflow-event:gate=review_request]")
        markers = extract_markers_from_note("13543", "75212", f"review posted {token}")
        self.assertEqual([(m.issue, m.journal, m.gate) for m in markers], [("13543", "75212", "review_request")])

    def test_review_result_alias_round_trips_to_review(self):
        token = render_workflow_event_marker("review_result")
        markers = extract_markers_from_note("13543", "75212", token)
        self.assertEqual(markers[0].gate, "review")  # review_result -> review runtime gate

    def test_optional_fields_are_emitted_and_read_back(self):
        token = render_workflow_event_marker("implementation_done", commit_bearing=True, issue_open=False)
        markers = extract_markers_from_note("13543", "75094", token)
        self.assertTrue(markers[0].commit_bearing)
        self.assertFalse(markers[0].issue_open)

    def test_non_gate_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            render_workflow_event_marker("reply")

    def test_blocked_is_callback_required_and_round_trips(self):
        # #13520 review F5: the callback-required vocabulary (workflow.md ### coordinator
        # callback を要する state) includes blocked — a coordinator must be woken on a blocker.
        token = render_workflow_event_marker("blocked")
        self.assertEqual(token, "[mozyo:workflow-event:gate=blocked]")
        markers = extract_markers_from_note("13518", "75300", f"blocked on X {token}")
        self.assertEqual(markers[0].gate, "blocked")

    def test_owner_close_approval_waiting_round_trips_to_owner_close_approval(self):
        # #13520 review F5: the marker-facing owner_close_approval_waiting state maps onto the
        # runtime owner_close_approval gate.
        token = render_workflow_event_marker("owner_close_approval_waiting")
        markers = extract_markers_from_note("13518", "75301", token)
        self.assertEqual(markers[0].gate, "owner_close_approval")

    def test_still_rejects_non_callback_gate_close(self):
        # `close` reaches a terminal gate but is not a coordinator-callback state (the coordinator
        # drives close, it is not woken to it) — the producer must not mint a marker for it.
        with self.assertRaises(ValueError):
            render_workflow_event_marker("close")


if __name__ == "__main__":
    unittest.main()
