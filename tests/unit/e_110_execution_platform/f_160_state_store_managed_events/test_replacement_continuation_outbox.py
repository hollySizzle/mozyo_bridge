"""Exact-generation replacement continuation send reservation (Redmine #14741 R10)."""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from mozyo_bridge.core.state.replacement_continuation_outbox import (
    CONSUME_AUTHORITY_MOVED,
    CONSUME_DELIVERED,
    OUTBOX_DELIVERED,
    RESERVE_COMPLETED,
    RESERVE_GENERATION_MISMATCH,
    RESERVE_GRANTED,
    RESERVE_HELD,
    RESERVE_LEASE_LOST,
    RESERVE_POINTER_MISMATCH,
    ContinuationSendKey,
    ReplacementContinuationOutbox,
)
from mozyo_bridge.core.state.replacement_continuation_outbox_schema import (
    REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT,
    ReplacementContinuationOutboxError,
    TABLE,
)
from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionStore
from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
)


NOW = "2026-08-03T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
HOLDER = "replacement-holder"
GENERATION = 7


class ReplacementContinuationOutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = ReplacementTransactionStore(home=self.home)
        self.transaction_key = ReplacementTransactionKey("workspace-a", "action-a")
        self.store.plan_transaction(
            self.transaction_key,
            action_generation=GENERATION,
            decision=DecisionPointer("redmine", "14741", "97750"),
            continuation=ContinuationPointer(
                "redmine",
                "14741",
                "97748",
                "review_request",
                "redispatch_once",
            ),
            participants=(
                ParticipantPin(
                    lane_id="lane-a",
                    role="gateway",
                    provider="codex",
                    assigned_name="gateway-a",
                    old_locator="w1:p1",
                ),
            ),
            now=NOW,
        )
        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                "UPDATE replacement_transactions SET phase=?, revision=2, lease_holder=?, "
                "lease_epoch=1, lease_expires_at=? WHERE workspace_id=? AND action_id=?",
                (
                    PHASE_DRAINING_CONTINUATION,
                    HOLDER,
                    FUTURE,
                    *self.transaction_key.as_row(),
                ),
            )
        self.record = self.store.get(self.transaction_key)
        self.send_key = ContinuationSendKey.from_record(
            self.record, action_generation=GENERATION
        )
        self.outbox = ReplacementContinuationOutbox(self.store.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_one_exact_key_has_one_reservation_owner(self):
        first = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        second = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)

        self.assertEqual(first.disposition, RESERVE_GRANTED)
        self.assertTrue(first.token)
        self.assertEqual(second.disposition, RESERVE_HELD)
        self.assertFalse(second.token)

    def test_concurrent_reserve_has_one_winner(self):
        barrier = threading.Barrier(3)
        results = []

        def reserve():
            barrier.wait()
            results.append(self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW))

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(item.disposition == RESERVE_GRANTED for item in results), 1)
        self.assertEqual(sum(item.disposition == RESERVE_HELD for item in results), 1)

    def test_proven_zero_release_reverts_phase_and_allows_a_fresh_reserve(self):
        first = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)

        released = self.outbox.consume_reserved(
            self.send_key,
            first.token,
            holder=HOLDER,
            clock=lambda: NOW,
            authority_fn=lambda: False,
            send_fn=lambda: self.fail("authority refusal must be zero-send"),
            send_ok="ok",
            send_zero="zero",
        )

        self.assertEqual(released.disposition, CONSUME_AUTHORITY_MOVED)
        record = self.store.get(self.transaction_key)
        self.assertEqual(record.phase, PHASE_REPLACING_NONSELF)
        self.assertEqual(self.outbox.state_of(self.send_key), "")

        # This store-level test isolates the outbox from the transaction DAG's participant
        # prerequisite (covered by the replacement-transaction suite), so place the row back at
        # the already-established sendable fixture state directly.
        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                "UPDATE replacement_transactions SET phase=?, revision=revision+1 "
                "WHERE workspace_id=? AND action_id=?",
                (PHASE_DRAINING_CONTINUATION, *self.transaction_key.as_row()),
            )
        second = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        self.assertEqual(second.disposition, RESERVE_GRANTED)
        self.assertNotEqual(first.token, second.token)

        stale_release = self.outbox.consume_reserved(
            self.send_key,
            first.token,
            holder=HOLDER,
            clock=lambda: NOW,
            authority_fn=lambda: self.fail("a stale token owns no authority"),
            send_fn=lambda: self.fail("a stale token owns no send"),
            send_ok="ok",
            send_zero="zero",
        )
        self.assertEqual(stale_release.disposition, RESERVE_HELD)
        self.assertEqual(
            self.store.get(self.transaction_key).phase, PHASE_DRAINING_CONTINUATION
        )

    def test_started_send_is_non_reclaimable(self):
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        consumed = self.outbox.consume_reserved(
            self.send_key,
            reservation.token,
            holder=HOLDER,
            clock=lambda: NOW,
            authority_fn=lambda: True,
            send_fn=lambda: "ok",
            send_ok="ok",
            send_zero="zero",
        )

        replay = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)

        self.assertEqual(consumed.disposition, CONSUME_DELIVERED)
        self.assertEqual(replay.disposition, RESERVE_HELD)
        self.assertEqual(replay.state, OUTBOX_DELIVERED)

    def test_completion_writer_cannot_cross_final_validation_and_send(self):
        """The R10-F2 completion race is serialized behind the reserved transport."""

        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        send_entered = threading.Event()
        allow_send = threading.Event()
        completion_started = threading.Event()
        completion_done = threading.Event()
        order = []
        consumed = []

        def send():
            send_entered.set()
            self.assertTrue(allow_send.wait(2))
            order.append("send")
            return "ok"

        def consume():
            consumed.append(
                self.outbox.consume_reserved(
                    self.send_key,
                    reservation.token,
                    holder=HOLDER,
                    clock=lambda: NOW,
                    authority_fn=lambda: True,
                    send_fn=send,
                    send_ok="ok",
                    send_zero="zero",
                )
            )

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(send_entered.wait(2))

        def complete():
            completion_started.set()
            with sqlite3.connect(self.store.path, timeout=2) as conn:
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute(
                    "UPDATE replacement_transactions SET phase=? WHERE workspace_id=? "
                    "AND action_id=?",
                    (PHASE_COMPLETED, *self.transaction_key.as_row()),
                )
            order.append("completion")
            completion_done.set()

        completer = threading.Thread(target=complete)
        completer.start()
        self.assertTrue(completion_started.wait(2))
        self.assertFalse(completion_done.wait(0.05))

        allow_send.set()
        consumer.join(2)
        completer.join(2)

        self.assertEqual(consumed[0].disposition, CONSUME_DELIVERED)
        self.assertEqual(order, ["send", "completion"])
        self.assertTrue(completion_done.is_set())

    def test_atomic_reserve_preserves_typed_transaction_refusals(self):
        cases = (
            (
                "generation",
                dataclasses.replace(self.send_key, action_generation=GENERATION + 1),
                None,
                RESERVE_GENERATION_MISMATCH,
            ),
            (
                "pointer",
                dataclasses.replace(self.send_key, journal_id="97749"),
                None,
                RESERVE_POINTER_MISMATCH,
            ),
            ("lease", self.send_key, "foreign-holder", RESERVE_LEASE_LOST),
        )
        for label, send_key, holder, expected in cases:
            with self.subTest(label=label):
                result = self.outbox.reserve(
                    send_key, holder=holder or HOLDER, now=NOW
                )
                self.assertEqual(result.disposition, expected)

    def test_completed_transaction_wins_before_reservation(self):
        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                "UPDATE replacement_transactions SET phase=? WHERE workspace_id=? "
                "AND action_id=?",
                (PHASE_COMPLETED, *self.transaction_key.as_row()),
            )

        result = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)

        self.assertEqual(result.disposition, RESERVE_COMPLETED)
        self.assertEqual(self.outbox.state_of(self.send_key), "")

    def test_registered_component_refuses_an_exact_shape_drift(self):
        self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        with sqlite3.connect(self.store.path) as conn:
            version = conn.execute(
                "SELECT schema_version FROM state_schema_components WHERE component=?",
                (REPLACEMENT_CONTINUATION_OUTBOX_COMPONENT,),
            ).fetchone()
            self.assertEqual(version, (1,))
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN foreign_axis TEXT")

        with self.assertRaises(ReplacementContinuationOutboxError):
            self.outbox.state_of(self.send_key)


if __name__ == "__main__":
    unittest.main()
