"""herdr delivery ledger persistence (Redmine #13296).

Classical-school unit tests over the real home-scoped SQLite store: build a
record from a real :class:`DeliveryOutcome`, persist it, and observe the durable
contract — persist round-trip, both outcome systems (event rail
``turn_start_outcome`` #13255 AND queue-enter ``queue_enter_turn_start_observation``
#13292) recorded verbatim, redaction (no absolute path is ever baked into a row),
causality (append-only chaining on ``notification_marker``), and the schema /
recovery guard. No network, no external service.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_delivery_ledger import (
    BACKEND_HERDR,
    ENTRY_DELIVERY_OUTCOME,
    ENTRY_DISPOSITION,
    HERDR_DELIVERY_LEDGER_SCHEMA_VERSION,
    RAIL_EVENT,
    RAIL_OTHER,
    RAIL_QUEUE_ENTER,
    REDACTED_PATH,
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerError,
    HerdrDeliveryLedgerRecord,
    build_herdr_delivery_ledger_record,
    herdr_delivery_ledger_path,
    record_herdr_delivery,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    DeliveryOutcome,
)

# An absolute path shape the redaction contract forbids in a persisted row.
_SECRET_ABS_PATH = "/Users/someone/private/lane/worktree"

# The #13255 event-rail turn-start telemetry (tokens + numbers only).
_EVENT_TELEMETRY = {
    "outcome": "delivered_not_started",
    "snapshot_state": "awaiting_input",
    "wait_kind": "timeout",
    "enter_resends": 1,
    "reclassified_blocked": False,
}

# The #13292 queue-enter post-choreography observation (tokens + bool + numbers).
_QUEUE_ENTER_OBSERVATION = {
    "observation_kind": "post_choreography_snapshot",
    "source": "herdr_agent_get",
    "runtime_state": "busy",
    "read_ok": True,
    "read_reason": None,
    "poll_attempts": 1,
}


def _outcome(
    *,
    status="sent",
    reason="ok",
    receiver="claude",
    target="%18",
    turn_start_outcome=None,
    queue_enter_turn_start_observation=None,
    execution_root=None,
    next_action_owner="receiver",
):
    """A real DeliveryOutcome anchored on #13296, for the projection tests."""
    return DeliveryOutcome(
        status=status,
        reason=reason,
        receiver=receiver,
        target=target,
        source="redmine",
        anchor={"source": "redmine", "issue": "13296", "journal": "72839"},
        mode="standard",
        kind="implementation_request",
        next_action_owner=next_action_owner,
        next_action="pick up the prompt",
        notification_marker="mkr-13296-abc",
        execution_root=execution_root,
        turn_start_outcome=turn_start_outcome,
        queue_enter_turn_start_observation=queue_enter_turn_start_observation,
    )


class LedgerRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "herdr-delivery-ledger.sqlite"

    def test_persist_round_trip_preserves_every_field(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        record = build_herdr_delivery_ledger_record(
            _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
            provider="claude",
            recorded_at="2026-07-06T03:00:00+00:00",
        )
        appended = ledger.append(record)
        self.assertEqual(appended.recorded_at, "2026-07-06T03:00:00+00:00")

        rows = ledger.records_for_marker("mkr-13296-abc")
        self.assertEqual(len(rows), 1)
        got = rows[0]
        self.assertEqual(got.entry_kind, ENTRY_DELIVERY_OUTCOME)
        self.assertEqual(got.notification_marker, "mkr-13296-abc")
        self.assertEqual(got.receiver, "claude")
        self.assertEqual(got.provider, "claude")
        self.assertEqual(got.backend, BACKEND_HERDR)
        self.assertEqual(got.rail, RAIL_EVENT)
        self.assertEqual(got.target, "%18")
        self.assertEqual(got.source, "redmine")
        self.assertEqual(got.issue_id, "13296")
        self.assertEqual(got.journal_id, "72839")
        # The telemetry dict survives verbatim (ACK semantics not reinvented).
        self.assertEqual(got.turn_start_outcome, _EVENT_TELEMETRY)
        self.assertIsNone(got.queue_enter_observation)
        self.assertEqual(got.recorded_at, "2026-07-06T03:00:00+00:00")

    def test_status_and_reason_stored_verbatim(self) -> None:
        # A blocked/delivered_not_started outcome: the ledger stores the wire
        # (status, reason) exactly, adding no judgement of its own.
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(
                    status="blocked",
                    reason="turn_start_unconfirmed",
                    turn_start_outcome=dict(_EVENT_TELEMETRY),
                    next_action_owner="sender",
                ),
                provider="claude",
            )
        )
        got = ledger.recent()[0]
        self.assertEqual(got.status, "blocked")
        self.assertEqual(got.reason, "turn_start_unconfirmed")
        self.assertEqual(got.next_action_owner, "sender")


class BothOutcomeSystemsTest(unittest.TestCase):
    """Both #13255 event-rail and #13292 queue-enter telemetry classify + persist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"

    def test_event_rail_outcome_classifies_event_rail(self) -> None:
        record = build_herdr_delivery_ledger_record(
            _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY))
        )
        self.assertEqual(record.rail, RAIL_EVENT)
        self.assertEqual(record.backend, BACKEND_HERDR)
        self.assertEqual(record.turn_start_outcome, _EVENT_TELEMETRY)
        self.assertIsNone(record.queue_enter_observation)

    def test_queue_enter_outcome_classifies_queue_enter_rail(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        record = build_herdr_delivery_ledger_record(
            _outcome(
                reason="queue_enter",
                queue_enter_turn_start_observation=dict(_QUEUE_ENTER_OBSERVATION),
            )
        )
        self.assertEqual(record.rail, RAIL_QUEUE_ENTER)
        self.assertEqual(record.backend, BACKEND_HERDR)
        self.assertIsNone(record.turn_start_outcome)
        self.assertEqual(record.queue_enter_observation, _QUEUE_ENTER_OBSERVATION)
        # And it survives a persist round-trip verbatim.
        ledger.append(record)
        got = ledger.recent()[0]
        self.assertEqual(got.queue_enter_observation, _QUEUE_ENTER_OBSERVATION)

    def test_non_herdr_outcome_classifies_other_and_keeps_caller_backend(self) -> None:
        # No herdr telemetry: rail=other; backend is NOT auto-derived to herdr.
        record = build_herdr_delivery_ledger_record(
            _outcome(), backend="tmux"
        )
        self.assertEqual(record.rail, RAIL_OTHER)
        self.assertEqual(record.backend, "tmux")

    def test_non_herdr_outcome_without_backend_leaves_backend_none(self) -> None:
        record = build_herdr_delivery_ledger_record(_outcome())
        self.assertEqual(record.rail, RAIL_OTHER)
        self.assertIsNone(record.backend)


class RedactionTest(unittest.TestCase):
    """No absolute / private path can be baked into a persisted row (#13296)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"

    def test_execution_root_abs_path_never_reaches_the_record(self) -> None:
        # The outcome carries an execution_root with an absolute home path; the
        # whitelist projection must not copy it into the ledger record.
        outcome = _outcome(
            turn_start_outcome=dict(_EVENT_TELEMETRY),
            execution_root={"path": _SECRET_ABS_PATH, "kind": "worktree"},
        )
        record = build_herdr_delivery_ledger_record(outcome, provider="claude")
        self.assertNotIn(_SECRET_ABS_PATH, record.to_json())
        # Required identity fields ARE present (redaction did not gut the record).
        self.assertEqual(record.receiver, "claude")
        self.assertEqual(record.target, "%18")
        self.assertEqual(record.issue_id, "13296")

    def test_persisted_row_bytes_contain_no_abs_path(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(
                    turn_start_outcome=dict(_EVENT_TELEMETRY),
                    execution_root={"path": _SECRET_ABS_PATH},
                )
            )
        )
        raw = self.path.read_bytes()
        self.assertNotIn(_SECRET_ABS_PATH.encode(), raw)


class CausalityAppendOnlyTest(unittest.TestCase):
    """A retry / disposition is a NEW entry chained on the same marker (no UPDATE)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"

    def test_disposition_entry_chains_on_marker(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(
                    status="blocked",
                    reason="turn_start_unconfirmed",
                    turn_start_outcome=dict(_EVENT_TELEMETRY),
                )
            )
        )
        # A later disposition on the SAME send: appended, never mutating the first.
        ledger.append(
            HerdrDeliveryLedgerRecord(
                entry_kind=ENTRY_DISPOSITION,
                notification_marker="mkr-13296-abc",
                disposition="resend_scheduled",
            )
        )
        chain = ledger.records_for_marker("mkr-13296-abc")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].entry_kind, ENTRY_DELIVERY_OUTCOME)
        self.assertEqual(chain[0].status, "blocked")  # first entry unchanged
        self.assertEqual(chain[1].entry_kind, ENTRY_DISPOSITION)
        self.assertEqual(chain[1].disposition, "resend_scheduled")

    def test_records_for_issue_lookup(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY))
            )
        )
        self.assertEqual(len(ledger.records_for_issue("13296")), 1)
        self.assertEqual(ledger.records_for_issue("99999"), [])


class SchemaGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"

    def test_reads_empty_when_absent(self) -> None:
        self.assertEqual(HerdrDeliveryLedger(path=self.path).recent(), [])

    def test_newer_schema_version_fails_closed_on_write(self) -> None:
        # A file stamped with a future schema version is left untouched.
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {HERDR_DELIVERY_LEDGER_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with self.assertRaises(HerdrDeliveryLedgerError):
            HerdrDeliveryLedger(path=self.path).append(
                HerdrDeliveryLedgerRecord(notification_marker="m")
            )

    def test_newer_schema_version_degrades_reads_to_empty(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {HERDR_DELIVERY_LEDGER_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        self.assertEqual(HerdrDeliveryLedger(path=self.path).recent(), [])


class RecordBoundaryTest(unittest.TestCase):
    """The best-effort send-boundary append never raises into the caller."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"

    def test_record_herdr_delivery_appends_under_home(self) -> None:
        appended = record_herdr_delivery(
            _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
            provider="claude",
            home=self.home,
        )
        self.assertIsNotNone(appended)
        rows = HerdrDeliveryLedger(home=self.home).records_for_marker("mkr-13296-abc")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rail, RAIL_EVENT)
        # It landed at the home-scoped path.
        self.assertTrue(herdr_delivery_ledger_path(self.home).exists())

    def test_record_herdr_delivery_swallows_store_failure(self) -> None:
        # A store failure must not break the send that triggered the append.
        with patch.object(
            HerdrDeliveryLedger, "append", side_effect=sqlite3.DatabaseError("boom")
        ):
            result = record_herdr_delivery(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
                home=self.home,
            )
        self.assertIsNone(result)


class CallerSuppliedRedactionTest(unittest.TestCase):
    """j#72883 finding 1/2: retry / disposition are sanitized at the persist boundary."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"
        self.home = Path(self._tmp.name) / "home"

    def test_retry_abs_path_string_is_redacted_in_record_and_bytes(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        appended = ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
                retry={"marker_observed": True, "note": _SECRET_ABS_PATH},
            )
        )
        # The returned record and the persisted bytes both drop the path.
        self.assertEqual(appended.retry["note"], REDACTED_PATH)
        self.assertEqual(appended.retry["marker_observed"], True)
        self.assertNotIn(_SECRET_ABS_PATH.encode(), self.path.read_bytes())
        got = ledger.recent()[0]
        self.assertEqual(got.retry["note"], REDACTED_PATH)

    def test_disposition_abs_path_string_is_redacted(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            HerdrDeliveryLedgerRecord(
                entry_kind=ENTRY_DISPOSITION,
                notification_marker="m",
                disposition=_SECRET_ABS_PATH,
            )
        )
        self.assertEqual(ledger.recent()[0].disposition, REDACTED_PATH)
        self.assertNotIn(_SECRET_ABS_PATH.encode(), self.path.read_bytes())

    def test_plain_disposition_token_survives(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            HerdrDeliveryLedgerRecord(
                entry_kind=ENTRY_DISPOSITION,
                notification_marker="m",
                disposition="resend_scheduled",
            )
        )
        self.assertEqual(ledger.recent()[0].disposition, "resend_scheduled")

    def test_retry_path_shaped_key_is_redacted_in_record_and_bytes(self) -> None:
        # j#72889: a path-shaped dict KEY (not just a value) must not survive.
        ledger = HerdrDeliveryLedger(path=self.path)
        appended = ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
                retry={_SECRET_ABS_PATH: "value"},
            )
        )
        self.assertNotIn(_SECRET_ABS_PATH, appended.retry)
        self.assertIn(REDACTED_PATH, appended.retry)
        self.assertNotIn(_SECRET_ABS_PATH.encode(), self.path.read_bytes())
        got = ledger.recent()[0]
        self.assertNotIn(_SECRET_ABS_PATH, got.retry)

    def test_nested_retry_path_key_and_value_are_redacted(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
                retry={"outer": {_SECRET_ABS_PATH: _SECRET_ABS_PATH}},
            )
        )
        self.assertNotIn(_SECRET_ABS_PATH.encode(), self.path.read_bytes())

    def test_non_json_retry_value_never_raises_and_is_redacted(self) -> None:
        # A pathlib.Path in retry would make json.dumps raise TypeError; the
        # sanitizer coerces it, so the best-effort boundary returns a record.
        appended = record_herdr_delivery(
            _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY)),
            retry={"path": Path(_SECRET_ABS_PATH)},
            home=self.home,
        )
        self.assertIsNotNone(appended)
        self.assertEqual(appended.retry["path"], REDACTED_PATH)
        raw = herdr_delivery_ledger_path(self.home).read_bytes()
        self.assertNotIn(_SECRET_ABS_PATH.encode(), raw)


class RecordIdentityTest(unittest.TestCase):
    """j#72883 finding 3: the autoincrement id is exposed as durable record identity."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.sqlite"

    def test_append_returns_entry_id_and_reads_populate_it(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        unpersisted = build_herdr_delivery_ledger_record(
            _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY))
        )
        self.assertIsNone(unpersisted.entry_id)  # not yet persisted
        appended = ledger.append(unpersisted)
        self.assertIsInstance(appended.entry_id, int)
        self.assertIn("entry_id", appended.as_payload())
        # The read carries the same identity.
        got = ledger.records_for_marker("mkr-13296-abc")[0]
        self.assertEqual(got.entry_id, appended.entry_id)

    def test_distinct_entries_get_distinct_ids(self) -> None:
        ledger = HerdrDeliveryLedger(path=self.path)
        first = ledger.append(
            build_herdr_delivery_ledger_record(
                _outcome(turn_start_outcome=dict(_EVENT_TELEMETRY))
            )
        )
        second = ledger.append(
            HerdrDeliveryLedgerRecord(
                entry_kind=ENTRY_DISPOSITION,
                notification_marker="mkr-13296-abc",
                disposition="resend_scheduled",
            )
        )
        self.assertNotEqual(first.entry_id, second.entry_id)
        # The causality chain preserves ascending identity order.
        chain = ledger.records_for_marker("mkr-13296-abc")
        self.assertEqual([r.entry_id for r in chain], [first.entry_id, second.entry_id])


if __name__ == "__main__":
    unittest.main()
