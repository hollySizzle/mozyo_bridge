"""Blocker-claim grammar specifications (Redmine #15162 / #15163).

A ``blocked`` report is only as good as the record behind it. These pin that the
reader accepts exactly the governed parked-state blocker subset and nothing that
merely resembles it — a note that says ``blocked`` without a reason, without a
resume condition, or whose anchor points at a different record proves nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E402,E501
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.blocker_claim import (  # noqa: E402,E501
    latest_blocker_claim,
    read_blocker_claim,
)

ISSUE = "15151"
JOURNAL = "102123"

COMPLETE = """## Sublane park

- state: blocked
- durable_anchor: #15151 j#102123
- blocked_by: dependency #15149 is not closed
- resume_condition: close #15149 and re-dispatch the implementation_request
- resume_owner: coordinator
"""


def without(field: str) -> str:
    return "\n".join(
        line for line in COMPLETE.splitlines() if f"- {field}:" not in line
    )


class ReadBlockerClaimTests(unittest.TestCase):
    def test_a_complete_declaration_yields_a_claim(self) -> None:
        claim = read_blocker_claim(COMPLETE, issue_id=ISSUE, journal_id=JOURNAL)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.blocker_source, SOURCE_REDMINE)
        self.assertEqual(claim.reason, "dependency #15149 is not closed")
        self.assertIn("close #15149", claim.resume_condition)
        self.assertEqual(claim.durable_anchor, "#15151 j#102123")

    def test_a_note_that_is_not_a_park_declaration_yields_nothing(self) -> None:
        self.assertIsNone(
            read_blocker_claim(
                "## Gate: implementation_request\n\n- issue: #15151\n",
                issue_id=ISSUE,
                journal_id=JOURNAL,
            )
        )

    def test_each_missing_field_alone_defeats_the_claim(self) -> None:
        for field in ("state", "blocked_by", "resume_condition", "durable_anchor"):
            self.assertIsNone(
                read_blocker_claim(without(field), issue_id=ISSUE, journal_id=JOURNAL),
                field,
            )

    def test_prose_saying_blocked_is_not_a_declaration(self) -> None:
        self.assertIsNone(
            read_blocker_claim(
                "I think this lane is blocked and cannot continue right now.",
                issue_id=ISSUE,
                journal_id=JOURNAL,
            )
        )

    def test_a_conflicting_duplicate_field_defeats_the_claim(self) -> None:
        """A record that says two things proves neither."""
        doubled = COMPLETE + "\n- blocked_by: something entirely different\n"
        self.assertIsNone(
            read_blocker_claim(doubled, issue_id=ISSUE, journal_id=JOURNAL)
        )

    def test_an_anchor_naming_a_different_journal_is_refused(self) -> None:
        """A pointer at another record cannot supply this record's evidence."""
        self.assertIsNone(
            read_blocker_claim(COMPLETE, issue_id=ISSUE, journal_id="999999")
        )

    def test_an_anchor_naming_a_different_issue_is_refused(self) -> None:
        self.assertIsNone(
            read_blocker_claim(COMPLETE, issue_id="99999", journal_id=JOURNAL)
        )

    def test_a_malformed_anchor_is_refused(self) -> None:
        broken = COMPLETE.replace("#15151 j#102123", "issue 15151, journal 102123")
        self.assertIsNone(
            read_blocker_claim(broken, issue_id=ISSUE, journal_id=JOURNAL)
        )

    def test_a_non_blocked_state_is_not_a_blocker_claim(self) -> None:
        resumed = COMPLETE.replace("- state: blocked", "- state: active")
        self.assertIsNone(
            read_blocker_claim(resumed, issue_id=ISSUE, journal_id=JOURNAL)
        )

    def test_empty_notes_yield_nothing(self) -> None:
        self.assertIsNone(read_blocker_claim("", issue_id=ISSUE, journal_id=JOURNAL))


class LatestBlockerClaimTests(unittest.TestCase):
    def test_the_newest_declaration_wins(self) -> None:
        older = COMPLETE.replace(
            "j#102123", "j#100000"
        ).replace("dependency #15149 is not closed", "older reason")
        journals = [
            ("100000", older),
            (JOURNAL, COMPLETE),
        ]
        claim = latest_blocker_claim(journals, issue_id=ISSUE)
        self.assertEqual(claim.reason, "dependency #15149 is not closed")

    def test_a_later_unrelated_journal_does_not_hide_a_standing_block(self) -> None:
        journals = [
            (JOURNAL, COMPLETE),
            ("999999", "## Progress\n\n- note: still working\n"),
        ]
        self.assertIsNotNone(latest_blocker_claim(journals, issue_id=ISSUE))

    def test_no_declaration_anywhere_yields_nothing(self) -> None:
        journals = [("1", "## Gate: start"), ("2", "## Gate: implementation_done")]
        self.assertIsNone(latest_blocker_claim(journals, issue_id=ISSUE))

    def test_malformed_journal_entries_are_skipped_not_fatal(self) -> None:
        journals = [None, ("x",), (JOURNAL, COMPLETE)]
        self.assertIsNotNone(latest_blocker_claim(journals, issue_id=ISSUE))

    def test_an_empty_journal_list_yields_nothing(self) -> None:
        self.assertIsNone(latest_blocker_claim((), issue_id=ISSUE))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
