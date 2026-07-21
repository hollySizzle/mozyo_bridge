"""Pure dispatch-authorization parser + validity tests (Redmine #13489 increment 2).

Pins the dedicated ``[mozyo:dispatch-authorization:...]`` channel: it is distinct from the
handoff ``kind=implementation_request`` token, an authorization is *valid* only with every
required field + the exact authority values, and the builder round-trips into the parser.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
    build_dispatch_authorization_marker,
    parse_dispatch_authorizations,
)


@dataclass(frozen=True)
class _Entry:
    """Duck-typed RedmineJournalEntry (journal_id + notes)."""

    journal_id: str
    notes: str


def _valid_marker(**over: str) -> str:
    fields = dict(
        action_id="act-1",
        source_gate="74999",
        issue="13489",
        workspace_id="ws1",
        lane_id="issue_13489",
        target_assigned_name="mzb1_ws1_claude_issue_13489",
    )
    fields.update(over)
    return build_dispatch_authorization_marker(**fields)


class ParseTest(unittest.TestCase):
    def test_valid_marker_round_trips(self):
        note = "coordinator authorizes dispatch\n" + _valid_marker()
        auths = parse_dispatch_authorizations([_Entry("75010", note)])
        self.assertEqual(len(auths), 1)
        auth = auths[0]
        self.assertTrue(auth.valid)
        self.assertEqual(auth.action_id, "act-1")
        self.assertEqual(auth.journal, "75010")
        self.assertEqual(auth.target_assigned_name, "mzb1_ws1_claude_issue_13489")

    def test_handoff_implementation_request_is_not_an_authorization(self):
        # The implementation_request handoff must never be read as a dispatch authorization.
        note = "[mozyo:handoff:source=redmine:issue=13489:journal=75006:kind=implementation_request:to=claude]"
        self.assertEqual(parse_dispatch_authorizations([_Entry("75006", note)]), ())

    def test_note_ordered_multiple(self):
        e1 = _Entry("75010", _valid_marker(action_id="a1"))
        e2 = _Entry("75011", _valid_marker(action_id="a2"))
        auths = parse_dispatch_authorizations([e1, e2])
        self.assertEqual([a.action_id for a in auths], ["a1", "a2"])

    def test_empty_note_contributes_nothing(self):
        self.assertEqual(parse_dispatch_authorizations([_Entry("1", "")]), ())


class ValidityTest(unittest.TestCase):
    def _parse(self, marker: str) -> DispatchAuthorization:
        return parse_dispatch_authorizations([_Entry("1", marker)])[0]

    def test_wrong_action_is_invalid(self):
        self.assertFalse(self._parse(_valid_marker(action="retire_worker")).valid)

    def test_unauthorized_conclusion_is_invalid(self):
        self.assertFalse(self._parse(_valid_marker(conclusion="pending")).valid)

    def test_wrong_target_role_is_invalid(self):
        self.assertFalse(self._parse(_valid_marker(target_role="delegated_coordinator")).valid)

    def test_non_coordinator_authorizer_is_invalid(self):
        self.assertFalse(self._parse(_valid_marker(authorized_by_role="worker")).valid)

    def test_missing_required_field_is_invalid(self):
        # action_id blanked out -> a required field is missing.
        self.assertFalse(self._parse(_valid_marker(action_id="")).valid)


class CorrelationTest(unittest.TestCase):
    def test_matches_lane_and_target(self):
        auth = parse_dispatch_authorizations([_Entry("1", _valid_marker())])[0]
        self.assertTrue(auth.matches_lane(workspace_id="ws1", lane_id="issue_13489", issue="13489"))
        self.assertFalse(auth.matches_lane(workspace_id="ws2", lane_id="issue_13489", issue="13489"))
        self.assertTrue(auth.matches_target("mzb1_ws1_claude_issue_13489"))
        self.assertFalse(auth.matches_target("mzb1_ws1_claude_other"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
