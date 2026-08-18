"""CallbackRedriveUseCase unit tests (Redmine #15707; review j#108062 finding_redriveboundary).

Pins the application-service contract over a FAKE store port (no sqlite, no filesystem): the
dry-run lists and filters without mutating, the apply requires the complete one-row naming
before the store is ever consulted, and the store's typed disposition passes through
untouched.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_redrive import (
    REDRIVE_INVALID_ARGS,
    CallbackRedriveUseCase,
    RedriveApplyRequest,
)


def _row(issue="15700", journal="107938"):
    return CallbackOutboxRow(
        source="redmine", issue=issue, journal=journal, normalized_gate="review",
        callback_route="coordinator", state="dead_letter", attempts=3, max_attempts=3,
        send_attempted=False, notification_kind="review_result", notification_summary="",
        gate_mismatch=False, detail="zero-send: precondition_not_idle", payload="",
        workspace_id="ws1",
    )


class _FakeRedriveStore:
    """A spec-level fake port: records calls, returns canned rows / dispositions."""

    def __init__(self, rows=(), disposition="requeued"):
        self.rows = tuple(rows)
        self.disposition = disposition
        self.fingerprint_calls = []
        self.requeue_calls = []

    def dead_letter_fingerprints(self, *, workspace_id=None):
        self.fingerprint_calls.append(workspace_id)
        return self.rows

    def requeue_dead_letter(self, key, *, expect_fingerprint):
        self.requeue_calls.append((key, expect_fingerprint))
        return self.disposition


def _request(**over):
    base = dict(
        workspace_id="ws1", source="redmine", issue="15700", journal="107938",
        normalized_gate="review", callback_route="coordinator", expect_fingerprint="tok",
    )
    base.update(over)
    return RedriveApplyRequest(**base)


class DryRunTest(unittest.TestCase):
    def test_lists_the_partition_and_never_requeues(self):
        store = _FakeRedriveStore(rows=((_row(), "fp1"),))
        listing = CallbackRedriveUseCase(store).dry_run(workspace_id="ws1")
        self.assertEqual(store.fingerprint_calls, ["ws1"])
        self.assertEqual(store.requeue_calls, [])
        self.assertEqual(listing.workspace_id, "ws1")
        self.assertEqual([fp for _, fp in listing.rows], ["fp1"])

    def test_issue_filter_narrows_the_listing(self):
        store = _FakeRedriveStore(rows=((_row(issue="15700"), "a"), (_row(issue="15702"), "b")))
        listing = CallbackRedriveUseCase(store).dry_run(
            workspace_id="ws1", issue_filter="15702"
        )
        self.assertEqual([fp for _, fp in listing.rows], ["b"])


class ApplyTest(unittest.TestCase):
    def test_complete_naming_passes_the_store_disposition_through(self):
        store = _FakeRedriveStore(disposition="requeued")
        result = CallbackRedriveUseCase(store).apply(_request())
        self.assertEqual(result.disposition, "requeued")
        (key, fingerprint), = store.requeue_calls
        self.assertEqual(
            key.as_row(), ("redmine", "15700", "107938", "review", "coordinator", "ws1")
        )
        self.assertEqual(fingerprint, "tok")

    def test_incomplete_naming_refuses_before_the_store_is_consulted(self):
        store = _FakeRedriveStore()
        for missing in ("issue", "journal", "normalized_gate", "callback_route", "expect_fingerprint", "source"):
            result = CallbackRedriveUseCase(store).apply(_request(**{missing: ""}))
            self.assertEqual(result.disposition, REDRIVE_INVALID_ARGS, missing)
        self.assertEqual(store.requeue_calls, [])

    def test_store_refusals_pass_through_typed(self):
        for disposition in ("absent", "state_mismatch", "fingerprint_mismatch"):
            store = _FakeRedriveStore(disposition=disposition)
            result = CallbackRedriveUseCase(store).apply(_request())
            self.assertEqual(result.disposition, disposition)


if __name__ == "__main__":
    unittest.main()
