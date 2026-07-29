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


class MarkdownCharacterClassTest(unittest.TestCase):
    """Markdown's whitespace is U+0020 and U+0009 — not Python's ``\\s`` (#14584 j#91406 F1).

    ``\\s`` matches every Unicode space, so a non-breaking space read as "blank" ends a quotation
    that is still open, and one trailing a fence run closes a block that is still verbatim. Both
    hand back the text below them. The reach is wider than it looks: the same class decides blank
    lines, fence closers, and the interrupters that end a lazy blockquote.
    """

    # Escape sequences, never literal characters: an invisible fixture silently degrades
    # into a plain space and the test then asserts nothing (it did, mid-#14584 R4).
    NBSP, EM_SPACE, FORM_FEED, VTAB = "\u00a0", "\u2003", "\u000c", "\u000b"

    def _not_markdown_space(self):
        return (("NBSP", self.NBSP), ("EM SPACE", self.EM_SPACE),
                ("form feed", self.FORM_FEED), ("vertical tab", self.VTAB))

    def test_a_unicode_space_does_not_close_a_fence(self):
        for label, space in self._not_markdown_space():
            with self.subTest(label):
                self.assertEqual(_gates(f"```\n```{space}\n{MARKER}\n```"), ())

    def test_a_unicode_space_is_not_a_blank_line(self):
        for label, space in self._not_markdown_space():
            with self.subTest(label):
                self.assertEqual(_gates(f"> quoted\n{space}\n{MARKER}"), ())

    def test_a_unicode_space_does_not_make_an_interrupter(self):
        # `#<NBSP>head` is not an ATX heading, so it cannot end the blockquote's paragraph.
        for label, space in self._not_markdown_space():
            with self.subTest(label):
                self.assertEqual(_gates(f"> quoted\n#{space}head\n{MARKER}"), ())
                self.assertEqual(_gates(f"> quoted\n-{space}item\n{MARKER}"), ())

    def test_space_and_tab_still_do_all_three(self):
        # The paired positive. Narrowing the class must not stop Markdown's own spaces working.
        for label, space in (("space", " "), ("tab", "\t")):
            with self.subTest(label):
                self.assertEqual(_gates(f"```\nq\n```{space}\n{MARKER}"), ("review_request",))
                self.assertEqual(_gates(f"> quoted\n{space}\n{MARKER}"), ("review_request",))
                self.assertEqual(_gates(f"> quoted\n#{space}head\n{MARKER}"), ("review_request",))

    def test_crlf_notes_are_read_the_same_as_lf(self):
        # Load-bearing for the class above: Redmine returns CRLF. Without normalizing the line
        # ending, every "\r" would sit after a fence run and stop it closing — the scan would go
        # from leaking quotations to swallowing the whole note.
        self.assertEqual(_gates(f"```\r\nq\r\n```\r\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"```\r\n{MARKER}\r\n```"), ())

    def test_a_bare_carriage_return_refuses_the_whole_note(self):
        # CommonMark calls a lone CR a line ending; pandoc does not split on it. Each reading has
        # shapes the other refuses, so where they disagree the note's structure — and its
        # authorship — is renderer-dependent, and nothing in it is authority.
        self.assertEqual(_gates(f"> quoted\r#\thead\r{MARKER}"), ())
        self.assertEqual(_gates(f"prose\r{MARKER}"), ())


class ParagraphContinuationTest(unittest.TestCase):
    """An indented line inside an open paragraph is hanging indent, not a code block (§4.4).

    Indented code cannot interrupt a paragraph. Treating four columns as a block start regardless
    cut the paragraph in two, and a code span whose delimiters were on either side stopped covering
    the lines between them (#14584 j#91406 F2).
    """

    def test_hanging_indent_does_not_break_a_multi_line_span(self):
        for label, indent in (("four spaces", "    "), ("tab", "\t"), ("space+tab", " \t")):
            with self.subTest(label):
                self.assertEqual(_gates(f"`start\n{indent}continuation\n{MARKER}\nend`"), ())

    def test_a_marker_written_under_a_deep_indent_is_still_refused(self):
        # The paragraph now survives the indented line, but the LINE is still blanked: this module
        # has refused a marker written four columns deep since #14585, and that has not changed.
        self.assertEqual(_gates(f"- observed:\n    {MARKER}"), ())
        self.assertEqual(_gates(f"prose\n    {MARKER}"), ())

    def test_a_real_indented_code_block_still_starts_one(self):
        self.assertEqual(_gates(f"    {MARKER}"), ())

    def test_a_paragraph_after_a_blank_line_is_canonical_again(self):
        self.assertEqual(_gates(f"prose\n    indented\n\n{MARKER}"), ("review_request",))


class RawHtmlTest(unittest.TestCase):
    """Raw HTML reaches the renderer as markup, so it can quote a marker (#14584 j#91406 F3).

    R3 declared the contract in terms of what the renderer puts inside ``<code>`` / ``<pre>`` /
    ``<blockquote>`` and then did not model raw HTML at all. An HTML construct this module does not
    model falls to "quoted": the refusal is recoverable, handing authority to markup is not.
    """

    def test_inline_html_code_is_quoted(self):
        self.assertEqual(_gates(f"text <code>{MARKER}</code> text"), ())

    def test_html_blocks_are_quoted(self):
        for tag in ("pre", "code", "blockquote", "script", "style", "textarea"):
            with self.subTest(tag):
                self.assertEqual(_gates(f"<{tag}>\n{MARKER}\n</{tag}>"), ())

    def test_an_unclosed_quoting_tag_swallows_past_the_blank_line(self):
        # A blank line ends most HTML blocks, but it does not close the ELEMENT: everything after
        # an unclosed <code> is still inside it in the rendered document.
        self.assertEqual(_gates(f"<code>\nquoted\n\n{MARKER}"), ())

    def test_every_raw_html_token_type_is_refused(self):
        # Comments, processing instructions, declarations and CDATA are not tags, and a marker in
        # any of them renders as NOTHING — `pandoc -t plain` of a commented marker is just the
        # surrounding words. Invisible text became gate authority (#14584 j#91593 F2).
        for label, note in (
            ("comment block", f"<!--\n{MARKER}\n-->"),
            ("comment inline", f"text <!-- {MARKER} --> text"),
            ("processing instruction", f"<?php\n{MARKER}\n?>"),
            ("declaration", f"<!DOCTYPE\n{MARKER}\n>"),
            ("CDATA", f"<![CDATA[\n{MARKER}\n]]>"),
            ("attribute value", f'text <span title="{MARKER}">visible</span>'),
            ("unknown tag", f"<figure>\n{MARKER}\n</figure>"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_nesting_and_close_like_text_do_not_end_the_refusal(self):
        # Depth and tokenization are exactly what this module stopped trying to model: an inner
        # </tag>, or one written inside an attribute or a comment, used to end the quotation early
        # while the renderer kept the marker inside the outer element (#14584 j#91593 F3).
        for label, note in (
            ("nested block tags", f"<blockquote>\n<blockquote>\nq\n</blockquote>\n{MARKER}\n</blockquote>"),
            ("nested inline tags", f"text <code>outer <code>inner</code> {MARKER} </code>"),
            ("close-like text in an attribute", f'<pre>\n<b t="</pre>">x</b>\n{MARKER}\n</pre>'),
            ("close-like text in a comment", f"<pre>\n<!-- </pre> -->\n{MARKER}\n</pre>"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_markup_anywhere_refuses_the_rest_of_the_note(self):
        # The accepted cost of not tokenizing: this module cannot say where markup ENDS without
        # parsing it, so it refuses from where markup begins. Prose that merely mentions a tag pays
        # this too. Recoverable — the writer backticks the tag, or records above it.
        self.assertEqual(_gates(f"we render <div> here\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"<div>\nquoted\n</div>\n\n{MARKER}"), ())

    def test_the_refusal_starts_where_the_markup_does_and_not_before(self):
        # The bound that keeps the rule from being "refuse every note": a marker ABOVE the markup
        # is still the writer's own voice.
        self.assertEqual(_gates(f"{MARKER}\n\nwe render <div> here"), ("review_request",))

    def test_markup_inside_a_quotation_costs_nothing(self):
        # Raw HTML is decided on what the other rules left standing, so a tag inside a code span or
        # a fence is already blanked and never triggers the refusal.
        self.assertEqual(_gates(f"we render `<div>` here\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"```\n<div>\n```\n\n{MARKER}"), ("review_request",))

    def test_an_escaped_angle_bracket_is_not_markup(self):
        # A backslash-escaped `<` is a literal (§2.4), so it starts nothing.
        self.assertEqual(_gates(f"we render \\<div> here\n\n{MARKER}"), ("review_request",))


class LinkSyntaxTest(unittest.TestCase):
    """A link's destination, title, reference label and an image's alt text are not shown as prose.

    Same class as the raw-HTML findings — an INVISIBLE marker becoming a durable gate event. The
    first two shapes came out of the generated corpus rather than a review; the rest came from
    #14584 j#91682, after a version of this rule that DID tokenize closed a reference definition at
    the physical line end, counted every parenthesis into one depth, and knew nothing of quoted
    titles or angle-bracket destinations.

    So this refuses to the end of the paragraph and asks nothing about where the link ends. The
    paragraph bound is not a design preference either: refusing to the end of the NOTE was measured
    against live journals and lost seven real gate events, because ``[P1][documented_rule …]`` is
    ordinary review prose.
    """

    def test_a_marker_as_a_link_destination_is_not_canonical(self):
        self.assertEqual(_gates(f"[text]({MARKER})"), ())

    def test_a_marker_as_a_link_title_is_not_canonical(self):
        self.assertEqual(_gates(f'[text](http://example.com "{MARKER}")'), ())

    def test_a_marker_as_a_reference_label_is_not_canonical(self):
        self.assertEqual(_gates(f"[text][{MARKER}]"), ())

    def test_a_marker_in_a_reference_definition_is_not_canonical(self):
        self.assertEqual(_gates(f"[ref]: http://example.com {MARKER}"), ())

    def test_a_definition_hides_text_on_the_lines_below_it(self):
        # CommonMark 0.31.2 §4.7: the destination may start on the NEXT line and the title may span
        # several. Any rule that closes the region at the physical line end releases these.
        self.assertEqual(_gates(f'[foo]: /url\n  "{MARKER}"'), ())
        self.assertEqual(_gates(f"[foo]:\n/url/{MARKER}"), ())

    def test_parentheses_inside_a_title_or_an_angle_destination_do_not_end_it(self):
        # The two shapes a parenthesis-counting version released: a ")" inside a quoted title, and
        # one inside an angle-bracket destination.
        self.assertEqual(_gates(f'[text](url "before ) {MARKER} after")'), ())
        self.assertEqual(_gates(f'[text](<https://x/)> "{MARKER}")'), ())

    def test_a_marker_in_image_alt_text_is_not_canonical(self):
        # Alt text renders into an attribute, not into the document text — the same class as
        # `<span title="...">`, which this module already refused (#14584 j#91682 F2).
        self.assertEqual(_gates(f"![prefix {MARKER} suffix](image.png)"), ())

    def test_a_marker_as_link_TEXT_is_canonical(self):
        # The paired positive: link text is exactly what the reader sees.
        self.assertEqual(_gates(f"[{MARKER}](http://example.com)"), ("review_request",))

    def test_the_refusal_ends_with_the_paragraph(self):
        # The bound. A marker in the NEXT paragraph is the writer's own voice again — without this
        # the rule would quietly become "any note containing a link records nothing".
        self.assertEqual(
            _gates(f"see [docs](http://example.com)\n\n{MARKER}"), ("review_request",)
        )

    def test_a_marker_above_a_link_is_canonical(self):
        self.assertEqual(_gates(f"{MARKER}\n\nsee [docs](http://example.com)"), ("review_request",))

    def test_escaped_brackets_are_not_link_syntax(self):
        # `\\[` and `\\]` are literals (§2.4), so this renders as visible text and starts nothing.
        # (Without this case the "a backslash no longer shields the opener" mutation is invisible.)
        self.assertEqual(_gates(f"see \\[docs\\](http://x) and {MARKER}"), ("review_request",))

    def test_link_syntax_inside_a_quotation_costs_nothing(self):
        # Decided on what the earlier rules left standing, so a link inside a code span is gone.
        self.assertEqual(_gates(f"see `[docs](http://x)` here\n\n{MARKER}"), ("review_request",))


class BackslashEscapeTest(unittest.TestCase):
    """A backslash-escaped delimiter is a literal, not a delimiter (CommonMark 0.31.2 §2.4).

    Counting an escaped backtick as a run paired it with the REAL opener after it, so the span
    those two delimiters actually formed stopped being blanked and released its content
    (#14584 j#91593 F1).
    """

    def test_an_escaped_backtick_does_not_open_a_span(self):
        self.assertEqual(_gates(f"\\` x `{MARKER}`"), ())

    def test_an_escaped_backtick_does_not_pair_with_a_real_one(self):
        self.assertEqual(_gates(f"text \\`code` {MARKER}`"), ())

    def test_an_escaped_backtick_inside_a_span_is_span_content(self):
        # The paired positive: the span still closes on its real delimiter, so the marker after it
        # is the writer's own voice.
        self.assertEqual(_gates(f"`a\\`b` {MARKER}"), ("review_request",))

    def test_an_escaped_backslash_does_not_escape_the_backtick(self):
        # `\\` is a literal backslash; the backtick after it is still a delimiter.
        self.assertEqual(_gates(f"text \\\\`{MARKER}`"), ())


class PassOrderTest(unittest.TestCase):
    """A pass may not hide what a WIDER pass has not read yet (#14584 j#91735).

    Rule E refuses to the end of the note. The tail passes and the hanging-indent blanking refuse
    only to the end of a paragraph or a line, and every one of them is capable of erasing the very
    ``<code>`` that E would have refused the rest of the note for. Running the narrow ones first did
    exactly that, and markers below the markup came back as authority.

    The reported case was the link tail. Three more passes had the same reach, so these pin the
    invariant rather than the report: E is read on the output of the passes that hide what the
    renderer hides, and applied after the ones that hide more.
    """

    def test_a_tail_refusal_does_not_hide_markup_from_the_note_wide_one(self):
        for label, note in (
            ("link tail", f"see [docs](/u) <code>\nquoted\n\n{MARKER}"),
            ("unmatched backtick tail", f"see `literal <code> more\nquoted\n\n{MARKER}"),
            ("image tail", f"see ![a](i.png) <code>\nq\n\n{MARKER}"),
            ("hanging indent", f"prose\n    <code>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_a_link_of_its_own_does_not_look_like_markup_to_it(self):
        # The other direction of the same invariant, and the regression reading E early created:
        # an angle destination or a tag-shaped title is markup the RENDERER hides, so E must not
        # read it and refuse the rest of the note. This erased live gate events (#14584 j#91761).
        for label, note in (
            ("angle destination", f"see [docs](<https://example.com>)\n\n{MARKER}"),
            ("reference definition", f"[ref]: <https://example.com>\n\n{MARKER}"),
            ("image angle destination", f"![a](<https://x/i.png>)\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))

    def test_a_tag_shaped_title_is_a_declared_over_blank(self):
        # The renderer hides this one too, but nothing short of parsing the link proves it, and the
        # attempt to approximate the region masked real <script> openers behind lexical triggers
        # that were not links at all (#14584 j#91792). Refusing is the direction that can be undone.
        self.assertEqual(_gates(f'[text](http://x "<code>")\n\n{MARKER}'), ())

    def test_a_lexical_link_trigger_never_hides_real_markup(self):
        # The four triggers, none of them an actual link. The true hidden region is empty, so any
        # mask at all was too large — and a marker between <script> tags came back as a gate.
        for label, note in (
            ("]( trigger", f"prose ]( <script> )\n\n{MARKER}\n</script>"),
            ("![ trigger", f"prose ![ <script> ]\n\n{MARKER}\n</script>"),
            ("][ trigger", f"prose ][ <script> ]\n\n{MARKER}\n</script>"),
            ("]: trigger", f"prose ]: <script>\n\n{MARKER}\n</script>"),
            ("link shape that is not a link", f"[docs]( invalid <script> )\n\n{MARKER}\n</script>"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_an_autolink_is_a_link_and_not_markup(self):
        # What actually makes the angle-destination cases above work, and it holds wherever an
        # autolink appears — no claim about surrounding link syntax is needed (CommonMark §6.5).
        for label, note in (
            ("scheme autolink in prose", f"see <https://example.com> here\n\n{MARKER}"),
            ("email autolink in prose", f"mail <a@example.com> here\n\n{MARKER}"),
            ("mailto autolink", f"see <mailto:a@example.com> here\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))

    def test_a_marker_inside_an_autolink_is_not_a_declaration(self):
        # "Not markup" and "not authority" are two decisions. Excluding autolinks from rule E and
        # leaving them visible to the marker scan let a marker written into the URL become a gate
        # (#14584 j#91839) — while rule F refuses the very same marker in [text](URL). The renderer
        # cannot arbitrate this one: an autolink's URL is also its label, so the marker IS visible
        # text. What settles it is that a URL is not prose.
        for label, note in (
            ("URI autolink", "<https://example.test/%s>" % MARKER),
            ("mailto autolink", "<mailto:a@x.test?s=%s>" % MARKER),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_bytes_that_start_an_html_block_are_not_treated_as_an_autolink(self):
        # CommonMark settles block structure before inlines (§3), and an email local part may hold
        # `!`, `?` and `-` — so <!--a@b>, <?a@b> and <!A@b> satisfy the autolink grammar AND start
        # HTML blocks of type 2/3/4 at the head of a line. The block wins, and blanking them as
        # autolinks hid the opener from rule E (#14584 j#91863). Matching a grammar is not being
        # parsed by it.
        for label, note in (
            ("type 2 comment", f"<!--a@b>\n{MARKER}"),
            ("type 3 processing instruction", f"<?a@b>\n{MARKER}"),
            ("type 4 declaration", f"<!A@b> {MARKER}"),
            ("type 4, lower case letter", f"<!z@b> {MARKER}"),
            ("type 2 closed on its own line", f"<!--a@b>--> {MARKER}"),
            ("three spaces still starts a block", f"   <!--a@b>\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_a_list_marker_is_a_container_prefix_not_prose(self):
        # A block may begin at a list item's content column, not only at the physical head of a
        # line (CommonMark §3.2 / §5.2): prepending `- ` to <!--a@b> gives a list item whose content
        # is the same raw HTML block. Reading "block start" as "line head" blanked that opener as an
        # autolink, and a marker inside an unrendered block became a gate (#14584 j#91918).
        for label, note in (
            ("unordered type 2", f"- <!--a@b>\n  {MARKER}"),
            ("unordered type 3", f"- <?a@b>\n  {MARKER}"),
            ("unordered type 4", f"- <!A@b> {MARKER}"),
            ("ordered with a dot", f"1. <!--a@b>\n   {MARKER}"),
            ("ordered with a paren", f"1) <!--a@b>\n   {MARKER}"),
            ("star marker", f"* <!--a@b>\n  {MARKER}"),
            ("nested list", f"- - <!--a@b>\n    {MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())

    def test_container_nesting_is_recognized_at_every_level(self):
        # The case above does not actually exercise nesting: its marker line is four columns deep,
        # so the hanging-indent rule blanks it whichever way this predicate goes. With the marker in
        # the next paragraph, only the container prefix decides — one container level short and the
        # opener is blanked as an autolink instead.
        #
        # Declared over-blank: the list item ends at the blank line, so the renderer does show this
        # marker. Rule E refuses to the end of the note by design, and that is the recoverable side.
        self.assertEqual(_gates(f"- - <!--a@b>\n\n{MARKER}"), ())
        # ...and the paired positive: nesting does not make an ordinary autolink into a block.
        self.assertEqual(_gates(f"- - <!@b>\n\n{MARKER}"), ("review_request",))

    def test_leading_indent_is_bounded_after_each_step_not_before(self):
        # A tab at column 0, 1 or 2 lands on column 4. Checking the bound before applying the step
        # consumed it as indentation, so hanging indent inside an open paragraph turned an inline
        # autolink into a block opener and the gate below it was erased (#14584 j#91954 F1).
        for label, note in (
            ("tab", f"prose\n\t<!--a@b>\n\n{MARKER}"),
            ("space then tab", f"prose\n \t<!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))
        # ...and three columns still is a block start, since an HTML block may interrupt a paragraph.
        self.assertEqual(_gates(f"prose\n   <!--a@b>\n\n{MARKER}"), ())

    def test_an_ordered_marker_interrupts_a_paragraph_only_at_one(self):
        # CommonMark §5.2: an ordered list interrupts a paragraph only when it starts at 1. A `2.`
        # inside a paragraph is prose, so its "container prefix" is not one, and treating it as a
        # block start erased the gate below (#14584 j#91954 F2). Both sides, and the same marker at
        # the top of a note where it really does open a list.
        self.assertEqual(_gates(f"prose\n2. <!--a@b>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"prose\n2) <!--a@b>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"prose\n1. <!--a@b>\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"prose\n- <!--a@b>\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"2. <!--a@b>\n\n{MARKER}"), ())
        # The condition is the start NUMBER, not the digit. Every spelling of one interrupts, and
        # matching the literal "1" read `01.` as prose and let an unrendered block through
        # (#14584 j#91997) — so both sides are pinned by value rather than by text.
        for label, note in (
            ("leading zero", f"prose\n01. <!--a@b>\n\n{MARKER}"),
            ("nine digits", f"prose\n000000001. <!--a@b>\n\n{MARKER}"),
            ("paren delimiter", f"prose\n01) <!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())
        for label, note in (
            ("padded two", f"prose\n000000002. <!--a@b>\n\n{MARKER}"),
            ("ten", f"prose\n10. <!--a@b>\n\n{MARKER}"),
            ("ten digits is not a marker at all", f"prose\n0000000001. <!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))

    def test_an_ordered_marker_is_arabic_digits_only(self):
        # CommonMark's ordered marker is one to nine ARABIC digits (§5.2); Python's `\d` also
        # matches every other Unicode decimal digit, so these read as list containers and the gate
        # below them was erased (#14584 j#92045). Same shape as `\s` being wider than Markdown's
        # whitespace — the character classes have to be the specification's, not the language's.
        for label, note in (
            ("Arabic-Indic one", f"\u0661. <!--a@b>\n\n{MARKER}"),
            ("fullwidth one", f"\uff11. <!--a@b>\n\n{MARKER}"),
            ("Devanagari one", f"\u0967. <!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))
        # ...and the ASCII controls that really are lists still open a container.
        self.assertEqual(_gates(f"1. <!--a@b>\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"2. <!--a@b>\n\n{MARKER}"), ())

    def test_the_ninth_digit_is_the_last_one_a_marker_may_have(self):
        # The tenth-digit edge of the same rule. It does not show through the block-start test —
        # the container prefix caps at nine digits too, so both spellings fail it — but it does
        # show through paragraph GROUPING: nine digits interrupt, so the unmatched backtick above
        # stops refusing there; ten digits do not, so the refusal carries on.
        # (Declared over-blank on the second line: the tail rule refuses more than the renderer.)
        self.assertEqual(_gates(f"prose `x\n000000001. y\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"prose `x\n0000000001. y\n{MARKER}"), ())

    def test_the_indent_after_a_list_marker_is_measured_in_columns(self):
        # A list marker admits one to four COLUMNS after it (§5.2 rule 1). At five the content is an
        # indented code block inside the item (§5.2 rule 2 with §4.4) — visible code, not a block
        # start — and refusing it erased the gate below (#14584 j#91938). Both sides of 4/5, and
        # tabs, which is where counting characters instead of columns goes wrong.
        for label, note in (
            ("one space opens a block", f"- <!--a@b>\n\n{MARKER}"),
            ("four spaces still open one", f"-    <!--a@b>\n\n{MARKER}"),
            ("one tab is four columns", f"-\t<!--a@b>\n\n{MARKER}"),
            ("space plus tab is four columns", f"- \t<!--a@b>\n\n{MARKER}"),
            ("ordered marker, four spaces", f"1.    <!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ())
        for label, note in (
            ("five spaces is indented code", f"-     <!--a@b>\n\n{MARKER}"),
            ("six spaces is indented code", f"-      <!--a@b>\n\n{MARKER}"),
            ("two tabs is indented code", f"-\t\t<!--a@b>\n\n{MARKER}"),
            ("ordered marker, five spaces", f"1.     <!--a@b>\n\n{MARKER}"),
            # A marker with NO whitespace after it is not a marker at all, so what follows is prose
            # rather than an item's content column. (Without this the "zero columns is fine"
            # mutation goes undetected.)
            ("no whitespace after the marker", f"-<!--a@b>\n\n{MARKER}"),
            ("ordered, no whitespace", f"1.<!--a@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))

    def test_a_list_does_not_cost_the_gate_below_it(self):
        # The paired positive, in the direction that has already been lost twice: inside a list the
        # opener test is the same one, so an ordinary autolink is still just an autolink, and prose
        # that merely follows a bullet is not a block start at all.
        self.assertEqual(_gates(f"- <!@b>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"- <https://example.test/>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"- item <!--a@b>\n\n{MARKER}"), ("review_request",))

    def test_only_a_real_block_opener_counts(self):
        # The other side of the same boundary, and the direction that costs real authority: `<!`
        # opens a block only before `--` or an ASCII letter. `<!@b>`, `<!1@b>` and `<!-@b>` are
        # ordinary email autolinks, and refusing them erased the gate recorded below (#14584
        # j#91898). This boundary has been wrong in BOTH directions now, so real blocks and real
        # autolinks are pinned together — moving it either way turns one of these red.
        for label, note in (
            ("<! then @", f"<!@b>\n\n{MARKER}"),
            ("<! then a digit", f"<!1@b>\n\n{MARKER}"),
            ("<! then a hyphen", f"<!-@b>\n\n{MARKER}"),
        ):
            with self.subTest(label):
                self.assertEqual(_gates(note), ("review_request",))

    def test_the_same_bytes_mid_paragraph_are_an_autolink(self):
        # The boundary, both sides. Four columns of indent is an indented code block rather than an
        # HTML block, and anywhere but the head of a line these bytes really are an email autolink.
        self.assertEqual(_gates(f"x <!--a@b>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"    <!--a@b>\n\n{MARKER}"), ("review_request",))
        # The 0-3 column limit itself, exercised where the indent is NOT already a code block:
        # inside an open paragraph a fourth column is hanging indent, so these bytes stay inline.
        # (A block-start test at four spaces proves nothing — rule C blanks that line first.)
        self.assertEqual(_gates(f"prose\n    <!--a@b>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"prose\n   <!--a@b>\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"<https://example.test/>\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"<a@example.test>\n\n{MARKER}"), ("review_request",))

    def test_an_autolink_costs_nothing_to_what_follows_it(self):
        # Both controls the refusal has to keep: a marker recorded after an autolink is still the
        # writer's own voice, and a real tag after one still refuses the rest of the note.
        self.assertEqual(_gates(f"see <https://example.test/> here\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"see <https://example.test/> <code>\nq\n\n{MARKER}"), ())

    def test_markup_outside_the_link_region_still_refuses_the_note(self):
        # And the mask must stay SMALL enough: a tag past the link, or past a definition's
        # destination, is real raw HTML. `[r]: /u <code>` is not even a definition — a title has to
        # be quoted — so masking the whole line there would have hidden real markup.
        self.assertEqual(_gates(f"see [docs](/u) <code>\nq\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"[r]: /u <code>\nmore\n\n{MARKER}"), ())

    def test_an_unterminated_opener_masks_nothing_at_all(self):
        # With no closing token there is no region to approximate, and the smallest approximation
        # is the empty one — so E still sees the tag and still refuses. Masking to the paragraph
        # end here would hide real markup, which is the direction that cannot be recovered.
        # (Without this case the "unterminated masks to the paragraph end" mutation is invisible.)
        self.assertEqual(_gates(f"see [docs](/u <code>\nq\n\n{MARKER}"), ())
        self.assertEqual(_gates(f"see ![a](/i <code>\nq\n\n{MARKER}"), ())

    def test_a_closed_span_may_still_hide_markup_from_it(self):
        # The other side: a CLOSED span hides exactly what the renderer hides, so reading E after it
        # is correct and a backticked tag still costs nothing. Collapsing the two span passes into
        # one — the shape this fix separates — would break this or the cases above.
        self.assertEqual(_gates(f"we render `<div>` here\n\n{MARKER}"), ("review_request",))
        self.assertEqual(_gates(f"see `[docs](http://x)` here\n\n{MARKER}"), ("review_request",))


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
