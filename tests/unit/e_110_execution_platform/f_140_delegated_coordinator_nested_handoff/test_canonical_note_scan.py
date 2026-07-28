"""The shared quote-aware canonical marker scan (Redmine #14585).

The rule under test is one sentence: **a marker Markdown renders as quoted or verbatim is someone
showing what a decision looks like, not an agent recording one.** These tests exist because the rule
was previously implemented twice — once in the proxy rail, once not at all in the Redmine journal
reader — and the half that was missing let a quoted marker become gate authority on a fresh lane
(#14577 j#90416 F1).

Both directions are pinned deliberately. The refusals stop a quotation from becoming authority; the
positives stop the refusals from swallowing real markers, which is the failure mode a
"just be stricter" fix produces (a scan that recognizes nothing is trivially quote-safe and
completely useless).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_fields,
    canonical_note_lines,
    canonical_note_text,
    parse_marker_fields,
)

MARKER = "[mozyo:workflow-event:gate=review_request:head=abc123]"


def _gates(notes):
    return tuple(f.get("gate") for _c, f in canonical_marker_fields(notes))


class CanonicalPositiveTest(unittest.TestCase):
    """A canonical marker is still read. Refusing everything is not quote-safety."""

    def test_top_level_marker_is_canonical(self):
        self.assertEqual(_gates(MARKER), ("review_request",))

    def test_marker_after_prose_body_is_canonical(self):
        # The shape every canonical producer emits: prose, blank line, marker at column 0.
        self.assertEqual(_gates("## Gate: review_request\n\nsome prose\n\n" + MARKER), ("review_request",))

    def test_marker_at_end_of_a_top_level_line_is_canonical(self):
        self.assertEqual(_gates(f"recorded: {MARKER}"), ("review_request",))

    def test_up_to_three_spaces_of_indent_is_still_top_level(self):
        # Markdown's own boundary: four spaces starts a code block, three do not.
        self.assertEqual(_gates("   " + MARKER), ("review_request",))

    def test_two_markers_on_distinct_top_level_lines_are_both_read(self):
        note = MARKER + "\n" + "[mozyo:workflow-event:gate=implementation_done]"
        self.assertEqual(_gates(note), ("review_request", "implementation_done"))

    def test_unrecognized_channel_is_dropped(self):
        self.assertEqual(canonical_marker_fields("[mozyo:chatter:gate=review_request]"), ())

    def test_channel_subset_filters(self):
        note = MARKER + "\n[mozyo:handoff:source=redmine:issue=1:journal=2:kind=review_result:to=claude]"
        self.assertEqual(
            tuple(c for c, _f in canonical_marker_fields(note, channels={"handoff"})),
            ("handoff",),
        )

    def test_empty_note_yields_nothing(self):
        for empty in ("", None):
            with self.subTest(repr(empty)):
                self.assertEqual(canonical_marker_fields(empty), ())


class QuotedMarkerTest(unittest.TestCase):
    """Every shape Markdown renders as a quotation. Each was a live or near-live escape."""

    def test_inline_code_span(self):
        self.assertEqual(_gates(f"the grammar is `{MARKER}`"), ())

    def test_blockquote(self):
        # THE shape that reached live acceptance as authority (#14577 j#90392).
        self.assertEqual(_gates("> " + MARKER), ())

    def test_nested_blockquote(self):
        self.assertEqual(_gates("> > " + MARKER), ())

    def test_indented_blockquote(self):
        self.assertEqual(_gates("  > " + MARKER), ())

    def test_indented_code_block(self):
        self.assertEqual(_gates("    " + MARKER), ())

    def test_tab_indented_code_block(self):
        self.assertEqual(_gates("\t" + MARKER), ())

    def test_fenced_block(self):
        self.assertEqual(_gates("```\n" + MARKER + "\n```"), ())

    def test_tilde_fenced_block(self):
        self.assertEqual(_gates("~~~\n" + MARKER + "\n~~~"), ())

    def test_fenced_block_with_language(self):
        self.assertEqual(_gates("```text\n" + MARKER + "\n```"), ())

    def test_unclosed_fence_swallows_the_rest(self):
        # A half-open quotation is still a quotation: fail closed rather than resume mid-block.
        self.assertEqual(_gates("```\nquoting:\n" + MARKER), ())

    def test_marker_under_a_list_bullet_is_refused(self):
        # The accepted cost of the rule: four spaces under a bullet is an indented code block as
        # far as the scan is concerned. Recoverable — the writer records at column 0.
        self.assertEqual(_gates("- observed:\n    " + MARKER), ())

    def test_callback_record_echoing_a_landing_marker_is_not_a_gate(self):
        # The concrete live journal shape: a record that reports what it saw, in backticks.
        note = (
            "## managed sublane dispatch outcome\n\n"
            "- observed landing marker: `%s`\n"
            "- current state: gateway_notified\n" % MARKER
        )
        self.assertEqual(_gates(note), ())


class DelimiterIdentityTest(unittest.TestCase):
    """A delimiter only delimits when it MATCHES (#14584 j#91152 F1, CommonMark 0.31.2 §4.5/§6.1).

    The shapes above are the quotations a reader recognizes on sight. These are the ones a reader
    also recognizes on sight but a *boolean* fence toggle and an "any two backticks" span do not:
    the block is verbatim in every renderer, and the scan used to hand it back as canonical.

    Refusing three reported shapes would have left four more standing (this class was written from
    the delimiter rules, not from the report — the escalation this US recorded). Each refusal is
    paired with the positive it must not swallow, because a scan that recognizes nothing is
    trivially quote-safe and completely useless.
    """

    def test_two_backtick_inline_span_is_quoted(self):
        # A one-backtick regex reads ``…`` as two EMPTY spans and leaves the middle canonical.
        self.assertEqual(_gates(f"the grammar is ``{MARKER}``"), ())

    def test_span_delimiters_must_be_the_same_length(self):
        # The run of 1 inside cannot close the run of 2 that opened the span.
        self.assertEqual(_gates(f"``a`b {MARKER}``"), ())

    def test_unmatched_backtick_string_refuses_the_rest_of_the_line(self):
        # No run of 2 ever closes this span. CommonMark renders it literally, but a line whose
        # quoting is unbalanced is the line whose authorship the scan cannot establish.
        self.assertEqual(_gates(f"``{MARKER}`"), ())

    def test_shorter_fence_run_inside_a_longer_fence_is_content(self):
        # ``` inside a ```` block does not close it, so the marker below is still inside.
        self.assertEqual(_gates(f"````\n```\n{MARKER}\n````"), ())

    def test_tilde_run_does_not_close_a_backtick_fence(self):
        self.assertEqual(_gates(f"```\n~~~\n{MARKER}\n```"), ())

    def test_backtick_run_does_not_close_a_tilde_fence(self):
        self.assertEqual(_gates(f"~~~\n```\n{MARKER}\n~~~"), ())

    def test_run_bearing_an_info_string_does_not_close_a_fence(self):
        # A closing fence may carry only whitespace; ```python is content.
        self.assertEqual(_gates(f"```\n```python\n{MARKER}\n```"), ())

    def test_backtick_in_an_info_string_means_the_line_never_opened_a_fence(self):
        # Without this rule ```a`b opens, and the REAL opener below reads as its closer — which
        # would release the fenced marker as canonical text.
        self.assertEqual(_gates(f"```a`b\n```\n{MARKER}\n```"), ())

    def test_longer_run_may_close_a_shorter_fence(self):
        # The other side of the length rule: >= opener length still closes, so the marker after it
        # is the writer's own voice and must be read.
        self.assertEqual(_gates(f"```\nquoted\n````\n{MARKER}"), ("review_request",))

    def test_marker_between_two_closed_spans_is_canonical(self):
        self.assertEqual(_gates(f"`a` {MARKER} ``b``"), ("review_request",))

    def test_marker_after_a_closed_two_backtick_span_is_canonical(self):
        self.assertEqual(_gates(f"``code`` {MARKER}"), ("review_request",))


class BlockStructureTest(unittest.TestCase):
    """Quotation is a property of the BLOCK, not of the line (#14584 j#91194 F1–F3).

    Three versions of this module asked each line about itself, and each one leaked the shape it did
    not ask about. Every case below was confirmed against a real CommonMark implementation (pandoc
    renders the marker inside ``<code>`` / ``<pre>`` / ``<blockquote>``) before it was pinned here —
    the enumeration is no longer the thing being trusted.

    Each refusal is paired with the positive one line away from it, because all three fixes work by
    carrying MORE state across lines, and that is exactly how a scan starts swallowing real markers.
    """

    # --- F1: a code span's delimiters can sit on different lines of one paragraph -----------
    def test_marker_inside_a_multi_line_code_span_is_quoted(self):
        # The middle line holds no backtick at all, so a per-line scan never looks at it.
        self.assertEqual(_gates(f"`start\n{MARKER}\nend`"), ())

    def test_marker_inside_a_multi_line_two_backtick_span_is_quoted(self):
        self.assertEqual(_gates(f"``start\n{MARKER}\nend``"), ())

    def test_a_span_cannot_reach_across_a_blank_line(self):
        # The paired positive: a blank line ends the paragraph, so the span never opens over it and
        # the marker below is the writer's own voice.
        self.assertEqual(_gates(f"`start\n\n{MARKER}"), ("review_request",))

    def test_marker_after_a_closed_multi_line_span_is_canonical(self):
        self.assertEqual(_gates(f"`start\nend`\n\n{MARKER}"), ("review_request",))

    # --- F2: indentation is measured in columns, and a tab advances to the next stop --------
    def test_space_then_tab_reaching_four_columns_is_indented_code(self):
        for spaces in (" ", "  ", "   "):
            with self.subTest(repr(spaces + "\t")):
                self.assertEqual(_gates(f"{spaces}\t{MARKER}"), ())

    def test_three_columns_of_indent_is_still_top_level(self):
        # The paired positive on the other side of the same boundary.
        for indent in ("", " ", "  ", "   "):
            with self.subTest(repr(indent)):
                self.assertEqual(_gates(indent + MARKER), ("review_request",))

    # --- F3: a blockquote's paragraph continues into the line below it ----------------------
    def test_lazy_continuation_of_a_blockquote_is_quoted(self):
        # No blank line, so CommonMark renders this marker inside the blockquote (§5.1).
        self.assertEqual(_gates(f"> quoted grammar\n{MARKER}"), ())

    def test_lazy_continuation_survives_intermediate_lines(self):
        self.assertEqual(_gates(f"> quoted\nstill quoted\n{MARKER}"), ())

    def test_a_blank_line_ends_the_lazy_continuation(self):
        self.assertEqual(_gates(f"> quoted\n\n{MARKER}"), ("review_request",))

    def test_a_block_that_interrupts_the_paragraph_ends_the_quotation(self):
        # A heading / list / fence cannot be lazy continuation text, so it closes the blockquote and
        # what follows is top level. Without this the scan would swallow the rest of the note.
        for label, interrupter in (
            ("heading", "## Gate: review_request"),
            ("list item", "- item"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(f"> quoted\n{interrupter}\n{MARKER}"), ("review_request",))


class ScanIsPerLineTest(unittest.TestCase):
    """Blanking must not let a marker be spliced together across a quotation."""

    def test_marker_cannot_be_spliced_across_a_quoted_line(self):
        # The marker body grammar is `[^\]]*`, which spans newlines. Scanning the blanked note as
        # ONE string would let the unclosed `[mozyo:` below close on the `]` two lines down and
        # parse as a marker that no single line contains.
        note = "[mozyo:workflow-event:gate=review_request\n> quoted line\nstill prose]"
        self.assertEqual(_gates(note), ())

    # The blank line in these fixtures is load-bearing, not formatting. Without it the last line
    # lazily continues the blockquote's paragraph and is quoted too (#14584 j#91194 F3) — these two
    # cases used to assert the opposite, which is how a wrong fixture pinned a wrong scan.
    def test_quoted_line_keeps_its_position(self):
        # Blanking rather than deleting is what preserves that property.
        self.assertEqual(canonical_note_lines("a\n> quoted\n\nb"), ("a", "", "", "b"))

    def test_canonical_text_rejoins_the_same_lines(self):
        self.assertEqual(canonical_note_text("a\n> quoted\n\nb"), "a\n\n\nb")

    def test_fence_state_resumes_after_the_closer(self):
        note = "```\n" + MARKER + "\n```\n" + MARKER
        self.assertEqual(_gates(note), ("review_request",))


class MarkerFieldParsingTest(unittest.TestCase):
    def test_fields_are_parsed_and_stripped(self):
        self.assertEqual(
            parse_marker_fields("gate=review_request: head=abc "),
            {"gate": "review_request", "head": "abc"},
        )

    def test_component_without_equals_is_dropped(self):
        self.assertEqual(parse_marker_fields("gate=x:bare:y=1"), {"gate": "x", "y": "1"})

    def test_last_write_wins(self):
        self.assertEqual(parse_marker_fields("gate=a:gate=b"), {"gate": "b"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
