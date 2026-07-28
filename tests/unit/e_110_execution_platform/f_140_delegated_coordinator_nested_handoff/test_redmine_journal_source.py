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


class QuotedMarkerIsNotAGateTest(unittest.TestCase):
    """Redmine #14585: this reader used to accept a QUOTED marker as a durable gate event.

    The proxy rail learned to tell a decision from a quotation of one (#14546); this reader — the
    one ``workflow watch``, callback discovery and the ``workflow step`` anchor gate all go through
    — did not, so the same defect was still live on the sibling parser. In #14577 j#90416 F1 an
    inline-quoted marker in an older journal became a fresh lane's verified anchor.

    Both directions are pinned: a quotation is not a gate, and a real top-level marker still is.
    The full shape matrix lives in ``test_canonical_note_scan``; these pin that this reader is
    actually wired to it, which is the thing that was missing.
    """

    GATE = "[mozyo:workflow-event:gate=review_request:head=" + "a" * 40 + "]"

    def _gates(self, notes):
        return tuple(m.gate for m in extract_markers_from_note("14585", "90416", notes))

    def test_top_level_marker_is_still_a_gate(self):
        self.assertEqual(self._gates(self.GATE), ("review_request",))

    def test_inline_quoted_marker_is_not_a_gate(self):
        self.assertEqual(self._gates("the marker to write is `%s`" % self.GATE), ())

    def test_blockquoted_marker_is_not_a_gate(self):
        self.assertEqual(self._gates("> " + self.GATE), ())

    def test_fenced_marker_is_not_a_gate(self):
        self.assertEqual(self._gates("```\n" + self.GATE + "\n```"), ())

    def test_indented_code_marker_is_not_a_gate(self):
        self.assertEqual(self._gates("    " + self.GATE), ())

    def test_mismatched_delimiters_are_not_a_gate_either(self):
        # #14584 j#91152 F1 reaching THIS reader: the shapes a boolean fence toggle and a
        # one-backtick span let through are verbatim to every renderer, so they must be as
        # invisible here as the four shapes above. Wiring, not grammar — the matrix is in
        # ``test_canonical_note_scan.DelimiterIdentityTest``.
        for label, note in (
            ("two-backtick span", "``%s``" % self.GATE),
            ("shorter run inside a longer fence", "````\n```\n%s\n````" % self.GATE),
            ("tilde run inside a backtick fence", "```\n~~~\n%s\n```" % self.GATE),
            ("backtick run inside a tilde fence", "~~~\n```\n%s\n~~~" % self.GATE),
            ("run bearing an info string", "```\n```python\n%s\n```" % self.GATE),
            ("backtick in the opener info string", "```a`b\n```\n%s\n```" % self.GATE),
            ("unmatched backtick string", "``%s`" % self.GATE),
        ):
            with self.subTest(label):
                self.assertEqual(self._gates(note), ())

    def test_block_level_quotation_is_not_a_gate_either(self):
        # #14584 j#91194 F1-F3 reaching THIS reader: quotation that only a multi-LINE view can see.
        # Each shape was confirmed with a real CommonMark renderer; the matrix is in
        # ``test_canonical_note_scan.BlockStructureTest``.
        for label, note in (
            ("multi-line code span", "`start\n%s\nend`" % self.GATE),
            ("multi-line two-backtick span", "``start\n%s\nend``" % self.GATE),
            ("one space + tab indent", " \t%s" % self.GATE),
            ("three spaces + tab indent", "   \t%s" % self.GATE),
            ("blockquote lazy continuation", "> quoted grammar\n%s" % self.GATE),
            ("lazy continuation two lines down", "> quoted\nstill quoted\n%s" % self.GATE),
        ):
            with self.subTest(label):
                self.assertEqual(self._gates(note), ())

    def test_character_class_and_markup_quotation_is_not_a_gate_either(self):
        # #14584 j#91406 F1-F3 reaching THIS reader: quotation that survives because Markdown's
        # whitespace is narrower than Python's, because an indent inside a paragraph is not a code
        # block, or because the quoting is raw HTML. Confirmed against a real renderer; the matrix
        # is in ``test_canonical_note_scan``.
        nbsp = "\u00a0"  # escape, not a literal: an invisible fixture degrades silently
        for label, note in (
            ("NBSP after a fence run", "```\n```%s\n%s\n```" % (nbsp, self.GATE)),
            ("NBSP as a blank line", "> quoted\n%s\n%s" % (nbsp, self.GATE)),
            ("NBSP in an interrupter", "> quoted\n#%shead\n%s" % (nbsp, self.GATE)),
            ("bare carriage return", "> quoted\r#\thead\r%s" % self.GATE),
            ("hanging indent in a span", "`start\n    cont\n%s\nend`" % self.GATE),
            ("inline raw HTML code", "text <code>%s</code>" % self.GATE),
            ("raw HTML pre block", "<pre>\n%s\n</pre>" % self.GATE),
            ("raw HTML blockquote block", "<blockquote>\n%s\n</blockquote>" % self.GATE),
        ):
            with self.subTest(label):
                self.assertEqual(self._gates(note), ())

    def test_markup_and_escaped_delimiters_are_not_a_gate_either(self):
        # #14584 j#91593 F1-F3 reaching THIS reader. The comment / attribute shapes are the sharp
        # ones: `pandoc -t plain` renders the marker as nothing at all, so an INVISIBLE string was
        # becoming a durable gate event.
        for label, note in (
            ("html comment", "text <!-- %s --> text" % self.GATE),
            ("attribute value", 'text <span title="%s">visible</span>' % self.GATE),
            ("CDATA", "<![CDATA[\n%s\n]]>" % self.GATE),
            ("nested quoting tags", "<blockquote>\n<blockquote>\nq\n</blockquote>\n%s\n</blockquote>" % self.GATE),
            ("escaped backtick", "\\` x `%s`" % self.GATE),
            ("link destination", "[text](%s)" % self.GATE),
            ("link title", '[text](http://example.com "%s")' % self.GATE),
            ("definition title on the next line", '[foo]: /url\n  "%s"' % self.GATE),
            ("paren inside a quoted title", '[text](url "a ) %s b")' % self.GATE),
            ("image alt text", "![a %s b](img.png)" % self.GATE),
            # #14584 j#91735: a narrow tail refusal erasing the markup that the
            # note-wide one would have refused.
            ("link tail hiding a later tag", "see [d](/u) <code>\nq\n\n%s" % self.GATE),
            ("hanging indent hiding a later tag", "prose\n    <code>\n\n%s" % self.GATE),
        ):
            with self.subTest(label):
                self.assertEqual(self._gates(note), ())

    def test_a_link_in_the_body_does_not_erase_the_gate(self):
        # #14584 j#91761: reading rule E before the link pass made an angle destination or a
        # tag-shaped title look like raw HTML, and the gate below it disappeared note-wide.
        for label, note in (
            ("angle destination", "see [docs](<https://example.com>)\n\n%s" % self.GATE),
            ("tag-shaped title", '[text](http://x "<code>")\n\n%s' % self.GATE),
            ("reference definition", "[ref]: <https://example.com>\n\n%s" % self.GATE),
        ):
            with self.subTest(label):
                self.assertEqual(self._gates(note), ("review_request",))

    def test_a_gate_recorded_above_the_markup_still_counts(self):
        # The bound: refusing from where markup starts must not erase what came before it.
        self.assertEqual(self._gates(self.GATE + "\n\nwe render <div> here"), ("review_request",))

    def test_a_crlf_note_still_yields_its_gate(self):
        # Redmine returns CRLF. If normalization were missing, the strict whitespace class above
        # would stop every real fence closing and this reader would go blind.
        self.assertEqual(self._gates("```\r\nq\r\n```\r\n" + self.GATE), ("review_request",))

    def test_a_blank_line_still_releases_the_writers_own_voice(self):
        # The paired positive for all three: one blank line away, the marker is a gate again.
        self.assertEqual(self._gates("> quoted\n\n" + self.GATE), ("review_request",))

    def test_a_matching_delimiter_still_releases_the_writers_own_voice(self):
        # The paired positive: >= opener length closes, so this marker is NOT quoted.
        self.assertEqual(self._gates("```\nquoted\n````\n" + self.GATE), ("review_request",))

    def test_quotation_is_not_an_ambiguity_poison_either(self):
        # The other half of the contract: a quoted marker must not merely be *refused*, it must be
        # invisible. If it still counted as a second marker it would make the journal unusable —
        # the failure mode the proxy rail hit when "2+ candidates" met a quotation (#14546).
        note = self.GATE + "\n\nfor reference, the same token quoted: `%s`" % self.GATE
        self.assertEqual(self._gates(note), ("review_request",))

    def test_a_review_journal_discussing_the_contract_yields_no_gate(self):
        note = (
            "## Gate: review\n\n"
            "- 指摘事項 [事実]: the producer must emit\n"
            "```\n" + self.GATE + "\n```\n"
            "- but the observed note carried `%s` instead\n" % self.GATE
        )
        self.assertEqual(self._gates(note), ())

    def test_markers_from_source_is_quote_aware_end_to_end(self):
        source = MappingRedmineJournalSource(
            payload={
                "issue": {"id": "14585"},
                "journals": [{"id": 90416, "notes": "quoted: `%s`" % self.GATE}],
            }
        )
        self.assertEqual(markers_from_source(source, "14585"), ())


if __name__ == "__main__":
    unittest.main()
