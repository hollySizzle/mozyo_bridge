"""Redmine #13892 — the dispatch-disposition marker and its correlation (design j#80629).

A `delivered` dispatch row is a delivery ACK: it can neither be waved through (that makes a
delivery ACK stand in for completion) nor blocked forever (that makes any pair ever dispatched
to un-retirable). Discharge is therefore proven by a three-way correspondence on the source of
truth — AUTHORIZE -> later review_request -> later exact disposition — and by nothing else.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
    CORRELATION_AMBIGUOUS,
    CORRELATION_DISCHARGED,
    CORRELATION_OWED,
    MARKER_CHANNEL_DISPATCH_DISPOSITION,
    DispatchRowIdentity,
    correlate_dispatch_disposition,
    parse_dispatch_dispositions,
    render_dispatch_disposition_marker,
)

ISSUE, WS, LANE = "13999", "wsabc", "dogfood13892"
NAME = "mzb1_wsabc_claude_dogfood13892"
ACTION = "act-1"


@dataclass(frozen=True)
class Entry:
    issue_id: str
    journal_id: str
    notes: str


@dataclass(frozen=True)
class Auth:
    issue: str
    workspace_id: str
    lane_id: str
    target_assigned_name: str
    action_id: str


def _row(**kw):
    base = dict(
        issue=ISSUE, journal="100", workspace_id=WS, lane_id=LANE,
        target_assigned_name=NAME, action_id=ACTION,
    )
    base.update(kw)
    return DispatchRowIdentity(**base)


def _auth(**kw):
    base = dict(
        issue=ISSUE, workspace_id=WS, lane_id=LANE,
        target_assigned_name=NAME, action_id=ACTION,
    )
    base.update(kw)
    return Auth(**base)


def _marker(**kw):
    base = dict(
        action_id=ACTION, dispatch_journal="100", workspace_id=WS, lane_id=LANE,
        target_assigned_name=NAME, terminal_journal="200",
    )
    base.update(kw)
    return render_dispatch_disposition_marker(**base)


def _history(*, disposition_notes=None, review_journal="200"):
    return [
        Entry(ISSUE, "100", "[mozyo:handoff:kind=implementation_request] AUTHORIZE here"),
        Entry(ISSUE, review_journal, "## Gate: review_request"),
        Entry(ISSUE, "300", disposition_notes if disposition_notes else "unrelated"),
    ]


class MarkerShapeTest(unittest.TestCase):
    def test_fixed_fields_are_emitted_literally_not_chosen(self):
        text = _marker()
        self.assertIn("terminal_gate=review_request", text)
        self.assertIn("conclusion=discharged", text)
        self.assertIn("recorded_by_role=implementation_gateway", text)

    def test_the_issue_is_not_a_marker_field(self):
        """A marker that self-reported its issue could name someone else's."""
        self.assertNotIn("issue=", _marker())

    def test_a_blank_identity_is_refused_at_render(self):
        with self.assertRaises(ValueError):
            _marker(action_id="")

    def test_issue_and_journal_come_from_the_owning_entry(self):
        found = parse_dispatch_dispositions(Entry("777", "888", _marker()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].issue, "777")
        self.assertEqual(found[0].journal, "888")

    def test_an_incomplete_marker_is_dropped(self):
        entry = Entry(ISSUE, "300", f"[mozyo:{MARKER_CHANNEL_DISPATCH_DISPOSITION}:action_id=x]")
        self.assertEqual(parse_dispatch_dispositions(entry), ())

    def test_the_channel_is_not_a_gate_or_watcher_channel(self):
        """It explains a correlation; it must never become a callback / gate candidate."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            GATE_BEARING_KINDS,
            _RECOGNIZED_CHANNELS,
        )

        self.assertNotIn(MARKER_CHANNEL_DISPATCH_DISPOSITION, _RECOGNIZED_CHANNELS)
        self.assertNotIn(MARKER_CHANNEL_DISPATCH_DISPOSITION, GATE_BEARING_KINDS)


class CorrelationTest(unittest.TestCase):
    def _correlate(self, row=None, entries=None, auths=None, reviews=("200",)):
        return correlate_dispatch_disposition(
            row or _row(),
            entries if entries is not None else _history(disposition_notes=_marker()),
            authorize_journals=auths if auths is not None else {"100": _auth()},
            review_request_journals=reviews,
        )

    def test_the_positive_three_way_correspondence_discharges(self):
        self.assertEqual(self._correlate().state, CORRELATION_DISCHARGED)

    def test_no_disposition_is_owed_not_discharged(self):
        v = self._correlate(entries=_history())
        self.assertEqual(v.state, CORRELATION_OWED)

    # -- everything below must NOT discharge --------------------------------

    def test_a_partial_implementation_done_does_not_discharge(self):
        """j#80629: implementation_done is not terminal. This issue's own j#80627 is an
        implementation_done that says "partial, incomplete" — treating it as discharge would
        false-discharge on a journal the worker wrote while still owing work."""
        entries = [
            Entry(ISSUE, "100", "AUTHORIZE"),
            Entry(ISSUE, "200", "## Gate: implementation_done — partial. Not complete."),
            Entry(ISSUE, "300", "unrelated"),
        ]
        v = self._correlate(entries=entries, reviews=())
        self.assertNotEqual(v.state, CORRELATION_DISCHARGED)

    def test_a_review_request_alone_does_not_discharge(self):
        """The gate exists but nothing ties it to THIS action."""
        v = self._correlate(entries=_history())
        self.assertEqual(v.state, CORRELATION_OWED)

    def test_a_disposition_naming_a_journal_with_no_review_request_blocks(self):
        v = self._correlate(reviews=())
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_order_inversion_blocks(self):
        """dispatch -> review_request -> disposition is required."""
        entries = [
            Entry(ISSUE, "300", _marker()),          # disposition first
            Entry(ISSUE, "100", "AUTHORIZE"),
            Entry(ISSUE, "200", "## Gate: review_request"),
        ]
        self.assertEqual(self._correlate(entries=entries).state, CORRELATION_AMBIGUOUS)

    def test_an_old_action_marker_does_not_discharge_a_new_action(self):
        """changes_requested -> re-dispatch mints a new journal + action_id (j#80629).

        The round-1 disposition is real and valid; it simply says nothing about round 2.
        """
        entries = [
            Entry(ISSUE, "100", "AUTHORIZE round 1"),
            Entry(ISSUE, "200", "## Gate: review_request"),
            Entry(ISSUE, "300", _marker()),                 # round 1 discharged
            Entry(ISSUE, "500", "AUTHORIZE round 2"),       # re-dispatch after changes
        ]
        v = self._correlate(
            row=_row(journal="500", action_id="act-2"),
            entries=entries,
            auths={"100": _auth(), "500": _auth(action_id="act-2")},
        )
        self.assertEqual(v.state, CORRELATION_OWED, "round 1's proof is not round 2's")

    def test_a_dispatch_journal_absent_from_history_blocks(self):
        """The dispatch's own origin cannot be confirmed -> never discharged."""
        v = self._correlate(
            row=_row(journal="999", action_id="act-9"),
            auths={"999": _auth(action_id="act-9")},
        )
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_a_foreign_workspace_marker_does_not_discharge(self):
        entries = _history(disposition_notes=_marker(workspace_id="OTHER_WS"))
        self.assertEqual(self._correlate(entries=entries).state, CORRELATION_OWED)

    def test_a_foreign_target_marker_does_not_discharge(self):
        entries = _history(disposition_notes=_marker(target_assigned_name="mzb1_x_y_z"))
        self.assertEqual(self._correlate(entries=entries).state, CORRELATION_OWED)

    def test_an_authorize_naming_a_different_identity_blocks(self):
        v = self._correlate(auths={"100": _auth(action_id="someone-else")})
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_a_missing_authorize_blocks(self):
        self.assertEqual(self._correlate(auths={}).state, CORRELATION_AMBIGUOUS)

    def test_a_blank_action_id_blocks(self):
        self.assertEqual(self._correlate(row=_row(action_id="")).state, CORRELATION_AMBIGUOUS)

    def test_an_invalid_fixed_field_blocks(self):
        bad = (
            f"[mozyo:{MARKER_CHANNEL_DISPATCH_DISPOSITION}:action_id={ACTION}"
            f":dispatch_journal=100:workspace_id={WS}:lane_id={LANE}"
            f":target_assigned_name={NAME}:terminal_gate=implementation_done"
            ":terminal_journal=200:conclusion=discharged"
            ":recorded_by_role=implementation_gateway]"
        )
        v = self._correlate(entries=_history(disposition_notes=bad))
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_a_worker_recorded_disposition_blocks(self):
        bad = _marker().replace(
            "recorded_by_role=implementation_gateway", "recorded_by_role=implementation_worker"
        )
        v = self._correlate(entries=_history(disposition_notes=bad))
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_conflicting_dispositions_block(self):
        entries = [
            Entry(ISSUE, "100", "AUTHORIZE"),
            Entry(ISSUE, "200", "## Gate: review_request"),
            Entry(ISSUE, "250", "## Gate: review_request"),
            Entry(ISSUE, "300", _marker()),
            Entry(ISSUE, "310", _marker(terminal_journal="250")),
        ]
        v = self._correlate(entries=entries, reviews=("200", "250"))
        self.assertEqual(v.state, CORRELATION_AMBIGUOUS)

    def test_an_identical_retry_duplicate_is_deduped_not_ambiguous(self):
        entries = [
            Entry(ISSUE, "100", "AUTHORIZE"),
            Entry(ISSUE, "200", "## Gate: review_request"),
            Entry(ISSUE, "300", _marker()),
            Entry(ISSUE, "310", _marker()),  # same payload, recorded twice
        ]
        self.assertEqual(self._correlate(entries=entries).state, CORRELATION_DISCHARGED)


if __name__ == "__main__":
    unittest.main()


class OpsCorrelationTest(unittest.TestCase):
    """The REAL ops seam over the REAL marker parsers (not the pure domain in isolation).

    The pure correlation being right proves nothing about production if the ops layer feeds it
    the wrong index — the "probe the real entry point" rule.
    """

    def setUp(self):
        from pathlib import Path

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            build_dispatch_authorization_marker,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            RedmineJournalEntry,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )

        self.Entry = RedmineJournalEntry
        self.ops = LiveSessionRetireOps(repo_root=Path("."))
        self.auth = build_dispatch_authorization_marker(
            action_id=ACTION, source_gate="start", issue=ISSUE, workspace_id=WS,
            lane_id=LANE, target_assigned_name=NAME,
        )
        self.disp = _marker()

    def _source(self, entries):
        class Src:
            def read_entries(self, issue_id):
                return entries

        return Src()

    def _history(self, *, with_disposition=True, review="[mozyo:workflow-event:gate=review_request]"):
        e = [
            self.Entry(issue_id=ISSUE, journal_id="100", notes=self.auth),
            self.Entry(issue_id=ISSUE, journal_id="200", notes=review),
        ]
        if with_disposition:
            e.append(self.Entry(issue_id=ISSUE, journal_id="300", notes=self.disp))
        return e

    def _run(self, entries):
        self.ops._redmine_source = lambda: self._source(entries)
        return self.ops._durable_disposition(ISSUE, "100")

    def test_the_full_correspondence_discharges(self):
        self.assertIs(self._run(self._history()), True)

    def test_no_disposition_is_owed(self):
        self.assertIs(self._run(self._history(with_disposition=False)), False)

    def test_no_credentials_blocks(self):
        self.ops._redmine_source = lambda: None
        self.assertIsNone(self.ops._durable_disposition(ISSUE, "100"))

    def test_an_unreadable_source_blocks(self):
        class Boom:
            def read_entries(self, issue_id):
                raise RuntimeError("credential failure")

        self.ops._redmine_source = lambda: Boom()
        self.assertIsNone(self.ops._durable_disposition(ISSUE, "100"))

    def test_a_missing_authorize_blocks(self):
        entries = [self.Entry(issue_id=ISSUE, journal_id="300", notes=self.disp)]
        self.assertIsNone(self._run(entries))

    def test_a_partial_implementation_done_does_not_discharge(self):
        """j#80627-shaped: an implementation_done that says "partial, incomplete"."""
        entries = self._history(
            with_disposition=False,
            review="[mozyo:workflow-event:gate=implementation_done] partial; not complete",
        )
        self.assertIsNot(self._run(entries), True)

    def test_the_callback_outbox_is_not_consulted(self):
        """j#80620 ruled CallbackOutbox out as the disposition source.

        Pinned BEHAVIOURALLY: the store is made to explode if touched. An `inspect.getsource`
        check would pass on a docstring that merely mentions the name — the reviewer's point
        that a source-string assertion is not a guard.
        """
        from unittest import mock

        with mock.patch(
            "mozyo_bridge.core.state.callback_outbox.CallbackOutbox",
            side_effect=AssertionError("the disposition must not consult the callback outbox"),
        ):
            self.assertIs(self._run(self._history()), True)
