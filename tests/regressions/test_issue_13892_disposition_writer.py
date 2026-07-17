"""Redmine #13892 — the canonical dispatch-disposition writer (design j#80629).

The writer is the only sanctioned producer of the record the retire gate treats as proof, so
every check runs against a FRESH read of the source of truth and every refusal writes nothing.
A record this producer cannot justify is worse than no record.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
    REASON_AUTHORIZE_AMBIGUOUS,
    REASON_AUTHORIZE_MISMATCH,
    REASON_BLANK_IDENTITY,
    REASON_CONFLICTING_RECORD,
    REASON_NO_AUTHORIZE,
    REASON_NO_TERMINAL_GATE,
    REASON_ORDER_INVERTED,
    REASON_SOURCE_UNREADABLE,
    WRITE_ALREADY_RECORDED,
    WRITE_RECORDED,
    WRITE_REFUSED,
    record_dispatch_disposition,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
    build_dispatch_authorization_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
    render_dispatch_disposition_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)

ISSUE, WS, LANE = "13999", "wsabc", "dogfood13892"
NAME, ACTION = "mzb1_wsabc_claude_dogfood13892", "act-1"
REVIEW = "[mozyo:workflow-event:gate=review_request]"


def _auth(**kw):
    base = dict(
        action_id=ACTION, source_gate="start", issue=ISSUE, workspace_id=WS,
        lane_id=LANE, target_assigned_name=NAME,
    )
    base.update(kw)
    return build_dispatch_authorization_marker(**base)


class _Src:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return self._entries


class DispositionWriterTest(unittest.TestCase):
    def setUp(self):
        self.appended = []

    def _append(self, issue, text):
        self.appended.append((issue, text))

    def _history(self, *, extra=(), review=REVIEW, auth=None):
        e = [
            RedmineJournalEntry(issue_id=ISSUE, journal_id="100", notes=auth or _auth()),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=review),
        ]
        e.extend(extra)
        return e

    def _record(self, *, entries=None, source=None, **kw):
        base = dict(
            issue=ISSUE, dispatch_journal="100", terminal_journal="200", workspace_id=WS,
            lane_id=LANE, target_assigned_name=NAME, action_id=ACTION,
            source=source or _Src(entries if entries is not None else self._history()),
            append_note=self._append,
        )
        base.update(kw)
        return record_dispatch_disposition(**base)

    # -- the one positive path ---------------------------------------------

    def test_a_verified_discharge_is_recorded_once(self):
        r = self._record()
        self.assertEqual(r.state, WRITE_RECORDED)
        self.assertEqual(len(self.appended), 1)
        issue, text = self.appended[0]
        self.assertEqual(issue, ISSUE)
        self.assertIn("dispatch-disposition", text)
        self.assertIn("recorded_by_role=implementation_gateway", text)

    def test_an_identical_retry_is_idempotent_and_writes_nothing(self):
        prior = RedmineJournalEntry(
            issue_id=ISSUE, journal_id="300",
            notes=render_dispatch_disposition_marker(
                action_id=ACTION, dispatch_journal="100", workspace_id=WS, lane_id=LANE,
                target_assigned_name=NAME, terminal_journal="200",
            ),
        )
        r = self._record(entries=self._history(extra=(prior,)))
        self.assertEqual(r.state, WRITE_ALREADY_RECORDED)
        self.assertTrue(r.ok)
        self.assertEqual(self.appended, [], "an idempotent retry must not append")

    # -- every refusal is zero-write ---------------------------------------

    def _assert_refused(self, r, reason):
        self.assertEqual(r.state, WRITE_REFUSED)
        self.assertEqual(r.reason, reason)
        self.assertEqual(self.appended, [], "a refusal must write nothing")

    def test_a_conflicting_prior_record_is_refused(self):
        prior = RedmineJournalEntry(
            issue_id=ISSUE, journal_id="300",
            notes=render_dispatch_disposition_marker(
                action_id=ACTION, dispatch_journal="100", workspace_id=WS, lane_id=LANE,
                target_assigned_name=NAME, terminal_journal="999",  # different terminal
            ),
        )
        self._assert_refused(
            self._record(entries=self._history(extra=(prior,))), REASON_CONFLICTING_RECORD
        )

    def test_a_missing_authorize_is_refused(self):
        entries = [RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW)]
        self._assert_refused(self._record(entries=entries), REASON_NO_AUTHORIZE)

    def test_an_authorize_naming_another_action_is_refused(self):
        self._assert_refused(
            self._record(entries=self._history(auth=_auth(action_id="other"))),
            REASON_AUTHORIZE_MISMATCH,
        )

    def test_a_foreign_workspace_authorize_is_refused(self):
        self._assert_refused(
            self._record(entries=self._history(auth=_auth(workspace_id="OTHER"))),
            REASON_AUTHORIZE_MISMATCH,
        )

    def test_two_authorizes_at_one_journal_are_ambiguous(self):
        entries = [
            RedmineJournalEntry(
                issue_id=ISSUE, journal_id="100",
                notes=_auth() + "\n" + _auth(action_id="act-2"),
            ),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW),
        ]
        self._assert_refused(self._record(entries=entries), REASON_AUTHORIZE_AMBIGUOUS)

    def test_a_terminal_journal_without_a_review_request_is_refused(self):
        """implementation_done is not a terminal gate (j#80629)."""
        entries = self._history(
            review="[mozyo:workflow-event:gate=implementation_done] partial; not complete"
        )
        self._assert_refused(self._record(entries=entries), REASON_NO_TERMINAL_GATE)

    def test_a_terminal_gate_preceding_the_dispatch_is_refused(self):
        entries = [
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="100", notes=_auth()),
        ]
        self._assert_refused(self._record(entries=entries), REASON_ORDER_INVERTED)

    def test_an_unreadable_source_is_refused(self):
        class Boom:
            def read_entries(self, issue_id):
                raise RuntimeError("credential failure")

        self._assert_refused(self._record(source=Boom()), REASON_SOURCE_UNREADABLE)

    def test_a_blank_identity_is_refused(self):
        self._assert_refused(self._record(action_id=""), REASON_BLANK_IDENTITY)


class WriterReaderRoundTripTest(unittest.TestCase):
    """What the writer records must be exactly what the reader accepts as proof."""

    def test_a_recorded_disposition_discharges_the_reader(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
            CORRELATION_DISCHARGED,
            DispatchRowIdentity,
            correlate_dispatch_disposition,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
            parse_dispatch_authorizations,
        )

        appended = []
        entries = [
            RedmineJournalEntry(issue_id=ISSUE, journal_id="100", notes=_auth()),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW),
        ]
        r = record_dispatch_disposition(
            issue=ISSUE, dispatch_journal="100", terminal_journal="200", workspace_id=WS,
            lane_id=LANE, target_assigned_name=NAME, action_id=ACTION,
            source=_Src(entries),
            append_note=lambda i, t: appended.append((i, t)),
        )
        self.assertEqual(r.state, WRITE_RECORDED)
        # Feed exactly what the writer produced back to the reader.
        entries.append(
            RedmineJournalEntry(issue_id=ISSUE, journal_id="300", notes=appended[0][1])
        )
        auths = {}
        for a in parse_dispatch_authorizations(entries):
            if a.valid:
                auths.setdefault(a.journal, []).append(a)
        verdict = correlate_dispatch_disposition(
            DispatchRowIdentity(
                issue=ISSUE, journal="100", workspace_id=WS, lane_id=LANE,
                target_assigned_name=NAME, action_id=ACTION,
            ),
            entries,
            authorize_journals=auths,
            review_request_journals=["200"],
        )
        self.assertEqual(verdict.state, CORRELATION_DISCHARGED)


if __name__ == "__main__":
    unittest.main()
