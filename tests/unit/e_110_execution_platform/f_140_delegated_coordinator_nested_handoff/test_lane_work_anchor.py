"""The lane's exact current work anchor, joined on lane + generation (Redmine #14586).

The defect being replaced is a heuristic: with no exact binding to join on, the work anchor was
re-derived as *the latest gate-bearing marker anywhere in the issue's history*. On an issue that had
already been through review rounds that is a previous round's callback, so a fresh lane's own
dispatch lost to it (#14577 j#90416 F2 — lifecycle current dispatch j#90409, entrypoint answer R6's
callback).

So the central test here is not any single refusal; it is that a gate journal — however new, however
much it looks like the issue's latest word — is **not** a work anchor, and that the record naming
this lane and this generation is, however old.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_work_anchor import (  # noqa: E501
    WORK_ANCHOR_AMBIGUOUS,
    WORK_ANCHOR_FOREIGN,
    WORK_ANCHOR_MISSING,
    WORK_ANCHOR_RESOLVED,
    WORK_ANCHOR_STALE_GENERATION,
    WORK_ANCHOR_UNBOUND,
    resolve_lane_work_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
    render_dispatch_note,
    render_gate_note,
)

LANE = "issue_14584_quote_safe_work_anchor"


def _entry(journal_id, notes):
    return RedmineJournalEntry(issue_id="14584", journal_id=str(journal_id), notes=notes)


def _dispatch(journal_id, *, lane=LANE, generation=1, body="implementation request"):
    return _entry(journal_id, render_dispatch_note(body, lane=lane, lane_generation=generation))


def _gate(journal_id, gate="review_result", **kw):
    return _entry(journal_id, render_gate_note(gate, body="review outcome", **kw))


def _resolve(entries, *, lane=LANE, generation=1):
    return resolve_lane_work_anchor(entries, lane=lane, lane_generation=generation)


class ResolvedTest(unittest.TestCase):
    def test_single_dispatch_resolves_to_its_owning_entry(self):
        anchor = _resolve([_dispatch("90409")])
        self.assertEqual(anchor.status, WORK_ANCHOR_RESOLVED)
        self.assertTrue(anchor.resolved)
        # The OWNING entry's journal id, never the marker's self-report.
        self.assertEqual(anchor.journal, "90409")
        self.assertEqual((anchor.lane, anchor.lane_generation), (LANE, 1))

    def test_a_newer_gate_journal_does_not_outrank_the_dispatch(self):
        # The #14586 shape exactly: the issue's newest journal is a callback from a previous
        # round; the lane's own dispatch is older. The dispatch is still the work anchor.
        anchor = _resolve(
            [
                _gate("90200", target_head="a" * 40, review_request_journal="90100"),
                _dispatch("90409"),
                _gate("90416", target_head="b" * 40, review_request_journal="90409"),
            ]
        )
        self.assertEqual(anchor.journal, "90409")

    def test_gate_history_alone_is_not_a_work_anchor(self):
        anchor = _resolve([_gate("90416", target_head="b" * 40, review_request_journal="90100")])
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)
        self.assertEqual(anchor.journal, "")

    def test_same_entry_read_twice_is_one_dispatch(self):
        # A re-read of the same IR journal is the same dispatch, not a duplicate.
        anchor = _resolve([_dispatch("90409"), _dispatch("90409")])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))


class BindingTest(unittest.TestCase):
    """Without a lane AND a positive generation there is nothing exact to join on."""

    def test_blank_lane_is_unbound(self):
        anchor = _resolve([_dispatch("90409")], lane="")
        self.assertEqual(anchor.status, WORK_ANCHOR_UNBOUND)

    def test_zero_generation_is_unbound(self):
        anchor = _resolve([_dispatch("90409")], generation=0)
        self.assertEqual(anchor.status, WORK_ANCHOR_UNBOUND)

    def test_negative_generation_is_unbound(self):
        self.assertEqual(_resolve([_dispatch("90409")], generation=-1).status, WORK_ANCHOR_UNBOUND)

    def test_non_numeric_generation_is_unbound(self):
        self.assertEqual(_resolve([_dispatch("90409")], generation="two").status, WORK_ANCHOR_UNBOUND)

    def test_none_generation_is_unbound(self):
        self.assertEqual(_resolve([_dispatch("90409")], generation=None).status, WORK_ANCHOR_UNBOUND)

    def test_unbound_never_carries_a_journal(self):
        self.assertEqual(_resolve([_dispatch("90409")], generation=0).journal, "")


class FailClosedTest(unittest.TestCase):
    def test_no_entries_is_missing(self):
        self.assertEqual(_resolve([]).status, WORK_ANCHOR_MISSING)

    def test_prose_only_implementation_request_is_missing(self):
        # A legacy IR with no structured marker is never parse-guessed from its prose.
        anchor = _resolve([_entry("90409", "## Gate: Implementation Request\n\nplease implement")])
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)

    def test_quoted_dispatch_marker_is_missing(self):
        # Redmine #14585 reaching this join: a record echoing a dispatch marker is not a dispatch.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        for label, note in (
            ("inline code", "- observed landing marker: `%s`" % marker),
            ("blockquote", "> " + marker),
            ("fenced", "```\n" + marker + "\n```"),
            ("indented code", "    " + marker),
            # #14584 j#91152 F1: the same echo written with delimiters that do not match. Every
            # one of these is verbatim in the rendered journal, so none of them dispatched work.
            ("two-backtick span", "``%s``" % marker),
            ("shorter run inside a longer fence", "````\n```\n%s\n````" % marker),
            ("tilde run inside a backtick fence", "```\n~~~\n%s\n```" % marker),
            ("backtick run inside a tilde fence", "~~~\n```\n%s\n~~~" % marker),
            ("run bearing an info string", "```\n```python\n%s\n```" % marker),
            ("backtick in the opener info string", "```a`b\n```\n%s\n```" % marker),
            ("unmatched backtick string", "``%s`" % marker),
            # #14584 j#91194 F1-F3: quotation only a multi-line view of the note can see. Each was
            # confirmed against a real CommonMark renderer before being pinned.
            ("multi-line code span", "`start\n%s\nend`" % marker),
            ("one space + tab indent", " \t%s" % marker),
            ("three spaces + tab indent", "   \t%s" % marker),
            ("blockquote lazy continuation", "> quoted grammar\n%s" % marker),
            ("lazy continuation two lines down", "> quoted\nstill quoted\n%s" % marker),
        ):
            with self.subTest(label):
                self.assertEqual(_resolve([_entry("90409", note)]).status, WORK_ANCHOR_MISSING)

    def test_a_real_dispatch_one_blank_line_below_a_quotation_still_resolves(self):
        # The paired positive at the join for F1-F3: the extra state these refusals carry across
        # lines must stop at the paragraph, or a real dispatch under a quoted example goes missing.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        anchor = _resolve([_entry("90409", "> quoted example\n\n" + marker)])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_character_class_and_markup_quotation_is_missing_too(self):
        # #14584 j#91406 F1-F3 at the join: each of these resolved to a work anchor before the fix.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        nbsp = "\u00a0"  # escape, not a literal: an invisible fixture degrades silently
        for label, note in (
            ("NBSP after a fence run", "```\n```%s\n%s\n```" % (nbsp, marker)),
            ("NBSP as a blank line", "> quoted\n%s\n%s" % (nbsp, marker)),
            ("NBSP in an interrupter", "> quoted\n#%shead\n%s" % (nbsp, marker)),
            ("bare carriage return", "> quoted\r#\thead\r%s" % marker),
            ("hanging indent in a span", "`start\n    cont\n%s\nend`" % marker),
            ("inline raw HTML code", "text <code>%s</code>" % marker),
            ("raw HTML pre block", "<pre>\n%s\n</pre>" % marker),
            ("raw HTML blockquote block", "<blockquote>\n%s\n</blockquote>" % marker),
        ):
            with self.subTest(label):
                self.assertEqual(_resolve([_entry("90409", note)]).status, WORK_ANCHOR_MISSING)

    def test_markup_and_escaped_delimiters_are_missing_too(self):
        # #14584 j#91593 F1-F3 at the join: each of these resolved to a work anchor before the fix,
        # including the ones whose marker renders as nothing at all (comment / attribute).
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        for label, note in (
            ("html comment", "text <!-- %s --> text" % marker),
            ("attribute value", 'text <span title="%s">visible</span>' % marker),
            ("CDATA", "<![CDATA[\n%s\n]]>" % marker),
            ("nested quoting tags", "<blockquote>\n<blockquote>\nq\n</blockquote>\n%s\n</blockquote>" % marker),
            ("escaped backtick", "\\` x `%s`" % marker),
            ("link destination", "[text](%s)" % marker),
            ("link title", '[text](http://example.com "%s")' % marker),
            # #14584 j#91792: lexical triggers that are not links at all — a mask here
            # hid a real <script> opener and the marker inside it became a gate.
            ("]( trigger, not a link", "prose ]( <script> )\n\n%s\n</script>" % marker),
            ("][ trigger, not a link", "prose ][ <script> ]\n\n%s\n</script>" % marker),
            ("]: trigger, not a link", "prose ]: <script>\n\n%s\n</script>" % marker),
            ("![ trigger, not a link", "prose ![ <script> ]\n\n%s\n</script>" % marker),
            # #14584 j#91839: a URL is not prose, so a marker inside an autolink is no
            # more a declaration than one inside [text](URL).
            ("marker inside a URI autolink", "<https://example.test/%s>" % marker),
            ("marker inside a mailto autolink", "<mailto:a@x.test?s=%s>" % marker),
            # #14584 j#91863: the same bytes start an HTML block at the head of a line,
            # and the block phase wins — so they must reach the raw-HTML rule.
            ("html block type 2", "<!--a@b>\n%s" % marker),
            ("html block type 3", "<?a@b>\n%s" % marker),
            ("html block type 4", "<!A@b> %s" % marker),
            ("definition title on the next line", '[foo]: /url\n  "%s"' % marker),
            ("paren inside a quoted title", '[text](url "a ) %s b")' % marker),
            ("image alt text", "![a %s b](img.png)" % marker),
            # #14584 j#91735: a narrow tail refusal erasing the markup that the
            # note-wide one would have refused.
            ("link tail hiding a later tag", "see [d](/u) <code>\nq\n\n%s" % marker),
            ("hanging indent hiding a later tag", "prose\n    <code>\n\n%s" % marker),
        ):
            with self.subTest(label):
                self.assertEqual(_resolve([_entry("90409", note)]).status, WORK_ANCHOR_MISSING)

    def test_a_dispatch_recorded_above_the_markup_still_resolves(self):
        # The bound at the join: markup later in the note must not un-dispatch the work above it.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        anchor = _resolve([_entry("90409", marker + "\n\nwe render <div> here")])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_a_link_in_the_body_does_not_erase_the_work_anchor(self):
        # The same regression at the join: a dispatch record whose body merely contains a Markdown
        # link with an angle destination stopped resolving at all (#14584 j#91761).
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        for label, note in (
            ("angle destination", "see [docs](<https://example.com>)\n\n%s" % marker),
            ("reference definition", "[ref]: <https://example.com>\n\n%s" % marker),
        ):
            with self.subTest(label):
                anchor = _resolve([_entry("90409", note)])
                self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_a_crlf_dispatch_record_still_resolves(self):
        # The paired positive at the join: Redmine writes CRLF, so this is the ordinary case.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        anchor = _resolve([_entry("90409", "## Gate\r\n\r\n" + marker)])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_a_real_dispatch_after_a_closed_fence_still_resolves(self):
        # The paired positive at the join: refusing mismatched delimiters must not start refusing
        # dispatches that merely follow a quotation. A run at least as long as the opener closes it.
        marker = render_dispatch_note("", lane=LANE, lane_generation=1).strip()
        anchor = _resolve([_entry("90409", "```\nquoted\n````\n" + marker)])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_two_distinct_entries_are_ambiguous(self):
        anchor = _resolve([_dispatch("90409"), _dispatch("90410")])
        self.assertEqual(anchor.status, WORK_ANCHOR_AMBIGUOUS)
        self.assertEqual(anchor.journal, "")

    def test_other_lanes_dispatch_is_foreign(self):
        anchor = _resolve([_dispatch("90409", lane="issue_99999_other")])
        self.assertEqual(anchor.status, WORK_ANCHOR_FOREIGN)
        self.assertEqual(anchor.foreign_lanes, ("issue_99999_other",))

    def test_foreign_is_distinguished_from_a_plain_absence(self):
        # Both are zero-send; naming them apart is what stops an operator hunting for a record
        # that is not missing at all — it belongs to another lane.
        self.assertEqual(_resolve([]).foreign_lanes, ())
        self.assertNotEqual(_resolve([]).status, _resolve([_dispatch("1", lane="x")]).status)

    def test_own_lane_present_alongside_a_foreign_one_still_resolves(self):
        anchor = _resolve([_dispatch("90409"), _dispatch("90500", lane="issue_99999_other")])
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_newer_generation_on_the_record_is_stale(self):
        anchor = _resolve([_dispatch("90409", generation=2)], generation=1)
        self.assertEqual(anchor.status, WORK_ANCHOR_STALE_GENERATION)
        self.assertEqual(anchor.latest_generation, 2)

    def test_stale_wins_even_when_this_round_still_resolves(self):
        # The load-bearing ordering: a lane holding a perfectly resolvable round-1 anchor must
        # still stop once round 2 has been opened, or it keeps working superseded instructions.
        anchor = _resolve(
            [_dispatch("90409", generation=1), _dispatch("90500", generation=2)], generation=1
        )
        self.assertEqual(anchor.status, WORK_ANCHOR_STALE_GENERATION)
        self.assertEqual(anchor.journal, "")

    def test_a_newer_generation_of_another_lane_is_not_stale(self):
        # Generations are per lane; a sibling lane's round 5 says nothing about this lane's round 1.
        anchor = _resolve(
            [_dispatch("90409"), _dispatch("90500", lane="issue_99999_other", generation=5)]
        )
        self.assertEqual((anchor.status, anchor.journal), (WORK_ANCHOR_RESOLVED, "90409"))

    def test_generation_ahead_of_the_record_is_missing_not_resolved(self):
        anchor = _resolve([_dispatch("90409", generation=1)], generation=3)
        self.assertEqual(anchor.status, WORK_ANCHOR_MISSING)


class ResultShapeTest(unittest.TestCase):
    def test_only_a_resolved_anchor_carries_a_journal(self):
        # Carrying a "best guess" journal alongside a failure status is how a caller ends up
        # using one.
        for entries, generation in (
            ([], 1),
            ([_dispatch("1"), _dispatch("2")], 1),
            ([_dispatch("1", generation=2)], 1),
            ([_dispatch("1", lane="other")], 1),
            ([_dispatch("1")], 0),
        ):
            with self.subTest(str(entries) + f" gen={generation}"):
                anchor = _resolve(entries, generation=generation)
                self.assertNotEqual(anchor.status, WORK_ANCHOR_RESOLVED)
                self.assertEqual(anchor.journal, "")
                self.assertFalse(anchor.resolved)

    def test_none_entries_is_tolerated(self):
        self.assertEqual(resolve_lane_work_anchor(None, lane=LANE, lane_generation=1).status,
                         WORK_ANCHOR_MISSING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
