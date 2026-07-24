"""Hibernate redrive-intent store tests (Redmine #14219 T2c review j#86776 R5-F3).

The intent is the durable memory a crash-window redrive reconstructs the proven basis from,
instead of fabricating ``review_approved=True`` from the generic hibernated disposition. These
tests probe it adversarially: a round-trip that preserves the derived flags, an absent intent
(the normal "no intent" case a dependency-park / manual / pre-R5 row shows), the row-match
predicate that gates a redrive, a corrupt flags blob (must fail closed, never a satisfied
basis), and a foreign schema version (must fail closed, never rewrite).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.hibernate_redrive_intent import (
    HibernateRedriveIntentError,
    HibernateRedriveIntentStore,
    RedriveIntent,
)

_FLAGS = {
    "explicitly_parked": False,
    "review_approved": True,
    "staging_integrated": True,
    "required_ci_green": True,
    "dogfood_delegated": True,
    "commits_pushed": True,
    "callbacks_drained": True,
    "no_review_pending": True,
    "no_owner_approval_pending": True,
    "no_integration_pending": True,
    "no_pending_prompt": True,
    "not_working": True,
    "worktree_clean": True,
    "boundary_recorded": False,
}


def _intent(**kw) -> RedriveIntent:
    base = dict(
        workspace_id="wsW",
        lane_id="lane_1",
        lane_generation=2,
        issue_id="500",
        decision_journal="84999",
        basis="early_hibernate",
        action_id="hibernate:lane_1",
        assertion_flags=dict(_FLAGS),
    )
    base.update(kw)
    return RedriveIntent(**base)


class HibernateRedriveIntentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = HibernateRedriveIntentStore(
            path=self.dir / "hibernate-redrive-intent.sqlite"
        )

    def test_round_trip_preserves_the_derived_flags(self) -> None:
        self.store.record(_intent())
        got = self.store.get("wsW", "lane_1", 2)
        self.assertIsNotNone(got)
        self.assertEqual(got.issue_id, "500")
        self.assertEqual(got.decision_journal, "84999")
        self.assertEqual(got.basis, "early_hibernate")
        self.assertEqual(got.action_id, "hibernate:lane_1")
        self.assertEqual(dict(got.assertion_flags), _FLAGS)
        # The booleans survive the JSON round-trip as booleans (not "true" strings).
        self.assertIs(got.assertion_flags["review_approved"], True)
        self.assertIs(got.assertion_flags["explicitly_parked"], False)

    def test_absent_intent_reads_none(self) -> None:
        # An absent DB is the normal pre-write state; an unrelated key is simply not found.
        self.assertIsNone(self.store.get("wsW", "lane_1", 2))
        self.store.record(_intent())
        self.assertIsNone(self.store.get("wsW", "lane_1", 3))  # other generation
        self.assertIsNone(self.store.get("wsW", "other", 2))  # other lane
        self.assertIsNone(self.store.get("other", "lane_1", 2))  # other workspace

    def test_record_is_idempotent_upsert(self) -> None:
        self.store.record(_intent(decision_journal="84999"))
        self.store.record(_intent(decision_journal="85555"))  # same key, newer decision
        got = self.store.get("wsW", "lane_1", 2)
        self.assertEqual(got.decision_journal, "85555")
        # Exactly one row for the key (no accumulation).
        conn = sqlite3.connect(self.store.path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM hibernate_redrive_intent"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_matches_row_requires_issue_journal_and_action(self) -> None:
        intent = _intent()
        self.assertTrue(
            intent.matches_row(
                issue_id="500", decision_journal="84999", action_id="hibernate:lane_1"
            )
        )
        # Each axis independently breaks the match (a different cycle).
        self.assertFalse(
            intent.matches_row(
                issue_id="501", decision_journal="84999", action_id="hibernate:lane_1"
            )
        )
        self.assertFalse(
            intent.matches_row(
                issue_id="500", decision_journal="85555", action_id="hibernate:lane_1"
            )
        )
        self.assertFalse(
            intent.matches_row(
                issue_id="500", decision_journal="84999", action_id="hibernate:other"
            )
        )

    def test_a_corrupt_flags_blob_fails_closed(self) -> None:
        self.store.record(_intent())
        # Corrupt the stored flags to a non-JSON blob out of band.
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(
                "UPDATE hibernate_redrive_intent SET assertion_flags='not json{'"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.get("wsW", "lane_1", 2)

    def test_a_non_object_flags_blob_fails_closed(self) -> None:
        self.store.record(_intent())
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("UPDATE hibernate_redrive_intent SET assertion_flags='[1, 2, 3]'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.get("wsW", "lane_1", 2)

    def test_empty_workspace_or_lane_is_refused(self) -> None:
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.record(_intent(workspace_id="  "))
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.record(_intent(lane_id=""))

    def test_a_foreign_schema_version_fails_closed(self) -> None:
        self.store.record(_intent())
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("PRAGMA user_version = 9999")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.get("wsW", "lane_1", 2)
        with self.assertRaises(HibernateRedriveIntentError):
            self.store.record(_intent())  # a write must also refuse to rewrite it


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
