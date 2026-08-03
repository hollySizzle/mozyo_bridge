"""Exact-generation replacement continuation send reservation (Redmine #14741 R10)."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.replacement_continuation_outbox import (
    CONSUME_AUTHORITY_MOVED,
    CONSUME_DELIVERED,
    OUTBOX_DELIVERED,
    OUTBOX_RESERVED,
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
from mozyo_bridge.core.state import replacement_transaction_action_fence as action_fence_module
from mozyo_bridge.core.state.replacement_transaction_model import (
    PARTICIPANT_REPLACED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    encode_participants,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (
    REPLACEMENT_TRANSACTION_COMPONENT,
    REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE,
    TABLE as REPLACEMENT_TRANSACTION_TABLE,
    migrate_replacement_transaction_schema_v2,
)


NOW = "2026-08-03T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
NEAR_EXPIRY = "2026-08-03T00:00:05+00:00"
AFTER_EXPIRY = "2026-08-03T00:00:10+00:00"
HOLDER = "replacement-holder"
GENERATION = 7


def _downgrade_to_exact_v1(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name=?",
            (REPLACEMENT_TRANSACTION_TABLE,),
        ).fetchall():
            quoted = str(name).replace('"', '""')
            conn.execute(f'DROP TRIGGER "{quoted}"')
        conn.execute(f"DROP TABLE {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE}")
        conn.execute("DROP VIEW replacement_transactions")
        conn.execute(
            f"ALTER TABLE {REPLACEMENT_TRANSACTION_TABLE} "
            "RENAME TO replacement_transactions"
        )
        conn.execute(
            "UPDATE state_schema_components SET schema_version=1 WHERE component=?",
            (REPLACEMENT_TRANSACTION_COMPONENT,),
        )


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
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET phase=?, revision=2, lease_holder=?, "
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
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET phase=?, revision=revision+1 "
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

        participants = tuple(
            participant.with_phase(PARTICIPANT_REPLACED)
            for participant in self.record.participants
        )
        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET participants_manifest=? "
                "WHERE workspace_id=? AND action_id=?",
                (encode_participants(participants), *self.transaction_key.as_row()),
            )
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
            outcome = self.store.transition_phase(
                self.transaction_key,
                expected_revision=self.record.revision,
                expected_action_generation=GENERATION,
                target=PHASE_COMPLETED,
                holder=HOLDER,
                now=NOW,
            )
            self.assertTrue(outcome.applied)
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

    def test_slow_transport_does_not_block_an_unrelated_action_write(self):
        """R11-F2: the external effect holds no global state.sqlite writer lock."""

        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        send_entered = threading.Event()
        allow_send = threading.Event()
        unrelated_done = threading.Event()
        consumed = []

        def slow_send():
            send_entered.set()
            self.assertTrue(allow_send.wait(2))
            return "ok"

        def consume():
            consumed.append(
                self.outbox.consume_reserved(
                    self.send_key,
                    reservation.token,
                    holder=HOLDER,
                    clock=lambda: NOW,
                    authority_fn=lambda: True,
                    send_fn=slow_send,
                    send_ok="ok",
                    send_zero="zero",
                )
            )

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(send_entered.wait(2))

        unrelated_key = ReplacementTransactionKey("workspace-b", "action-b")

        def write_unrelated():
            self.store.plan_transaction(
                unrelated_key,
                action_generation=1,
                decision=DecisionPointer("redmine", "14741", "97757"),
                continuation=ContinuationPointer(
                    "redmine", "14741", "97755", "review_result", "redispatch_once"
                ),
                participants=(
                    ParticipantPin(
                        lane_id="lane-b",
                        role="gateway",
                        provider="codex",
                        assigned_name="gateway-b",
                        old_locator="w2:p1",
                    ),
                ),
                now=NOW,
            )
            unrelated_done.set()

        writer = threading.Thread(target=write_unrelated)
        writer.start()
        self.assertTrue(unrelated_done.wait(0.5))
        self.assertFalse(allow_send.is_set())

        allow_send.set()
        consumer.join(2)
        writer.join(2)
        self.assertEqual(consumed[0].disposition, CONSUME_DELIVERED)
        self.assertIsNotNone(self.store.get(unrelated_key))

    def test_authority_latency_that_crosses_lease_expiry_is_zero_send(self):
        """R11-F1: a DB lock cannot substitute for action-time wall-clock authority."""

        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET lease_expires_at=? "
                "WHERE workspace_id=? AND action_id=?",
                (NEAR_EXPIRY, *self.transaction_key.as_row()),
            )
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        current = [NOW]
        sends = []

        def authority():
            current[0] = AFTER_EXPIRY
            return True

        consumed = self.outbox.consume_reserved(
            self.send_key,
            reservation.token,
            holder=HOLDER,
            clock=lambda: current[0],
            authority_fn=authority,
            send_fn=lambda: sends.append("sent") or "ok",
            send_ok="ok",
            send_zero="zero",
        )

        self.assertEqual(consumed.disposition, RESERVE_LEASE_LOST)
        self.assertEqual(sends, [])
        self.assertEqual(self.outbox.state_of(self.send_key), OUTBOX_RESERVED)
        self.assertEqual(
            self.store.get(self.transaction_key).phase, PHASE_DRAINING_CONTINUATION
        )

    def test_symlink_swap_after_lock_selection_is_zero_send(self):
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        target_home = self.home / "symlink-target-home"
        target_home.mkdir()
        target = ReplacementTransactionStore(home=target_home)
        target_key = ReplacementTransactionKey("workspace-target", "action-target")
        self.assertTrue(
            target.plan_transaction(
                target_key,
                action_generation=1,
                decision=DecisionPointer("redmine", "14741", "97771"),
                continuation=ContinuationPointer(
                    "redmine", "14741", "97770", "review_request", "redispatch_once"
                ),
                participants=(
                    ParticipantPin(
                        lane_id="lane-target",
                        role="gateway",
                        provider="codex",
                        assigned_name="gateway-target",
                        old_locator="w2:p1",
                    ),
                ),
                now=NOW,
            ).applied
        )
        target_revision = target.get(target_key).revision
        sends = []
        original_open = action_fence_module._open_lock

        def swap_after_selection(lock_path):
            fd = original_open(lock_path)
            os.replace(self.store.path, self.home / "source-before-symlink.sqlite")
            self.store.path.symlink_to(target.path)
            return fd

        with mock.patch.object(
            action_fence_module, "_open_lock", side_effect=swap_after_selection
        ):
            with self.assertRaises(ReplacementContinuationOutboxError):
                self.outbox.consume_reserved(
                    self.send_key,
                    reservation.token,
                    holder=HOLDER,
                    clock=lambda: NOW,
                    authority_fn=lambda: True,
                    send_fn=lambda: sends.append("sent") or "ok",
                    send_ok="ok",
                    send_zero="zero",
                )
        self.assertEqual(sends, [])
        self.assertEqual(target.get(target_key).revision, target_revision)

    def test_no_db_wait_remains_between_final_authority_lease_join_and_send(self):
        """R12-F1: a final DB connection may move authority/clock only after send."""

        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET lease_expires_at=? "
                "WHERE workspace_id=? AND action_id=?",
                (NEAR_EXPIRY, *self.transaction_key.as_row()),
            )
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        original_connect = self.outbox._connect
        connect_calls = []
        authority_live = [True]
        current = [NOW]
        sends = []

        def observed_connect():
            connect_calls.append("connect")
            if len(connect_calls) == 2:
                # This is the post-effect outcome write. In the buggy ordering it was a
                # pre-effect final validation and caused an unauthorized expired send.
                authority_live[0] = False
                current[0] = AFTER_EXPIRY
            return original_connect()

        self.outbox._connect = observed_connect

        def send():
            self.assertEqual(connect_calls, ["connect"])
            self.assertTrue(authority_live[0])
            self.assertEqual(current[0], NOW)
            sends.append("sent")
            return "ok"

        consumed = self.outbox.consume_reserved(
            self.send_key,
            reservation.token,
            holder=HOLDER,
            clock=lambda: current[0],
            authority_fn=lambda: authority_live[0],
            send_fn=send,
            send_ok="ok",
            send_zero="zero",
        )

        self.assertEqual(consumed.disposition, CONSUME_DELIVERED)
        self.assertEqual(sends, ["sent"])
        self.assertEqual(len(connect_calls), 2)

    def test_already_admitted_v1_writer_cannot_mutate_during_v2_send(self):
        """R13-F2: the DB trigger stops a v1 writer admitted before migration."""

        _downgrade_to_exact_v1(self.store.path)
        legacy = sqlite3.connect(self.store.path)
        self.addCleanup(legacy.close)
        self.assertEqual(
            legacy.execute(
                "SELECT typeof(schema_version), schema_version "
                "FROM state_schema_components WHERE component=?",
                (REPLACEMENT_TRANSACTION_COMPONENT,),
            ).fetchone(),
            ("integer", 1),
        )
        # The old process has now passed its one admission check and pauses without an
        # open SQLite transaction. The offline rail migrates while all actual mutations
        # are quiesced; this connection then represents the resumed old code path.
        migrate_replacement_transaction_schema_v2(self.store.path)

        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        send_entered = threading.Event()
        allow_send = threading.Event()
        consumed = []

        def send():
            send_entered.set()
            self.assertTrue(allow_send.wait(2))
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

        with self.assertRaisesRegex(sqlite3.OperationalError, "is a view"):
            legacy.execute(
                "UPDATE replacement_transactions SET revision=revision+1 "
                "WHERE workspace_id=? AND action_id=?",
                self.transaction_key.as_row(),
            )
        legacy.rollback()
        with sqlite3.connect(self.store.path) as fence_unaware_v2:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "replacement transaction effect fenced"
            ):
                fence_unaware_v2.execute(
                    f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET revision=revision+1 "
                    "WHERE workspace_id=? AND action_id=?",
                    self.transaction_key.as_row(),
                )

        self.assertEqual(
            self.store.get(self.transaction_key).phase, PHASE_DRAINING_CONTINUATION
        )
        allow_send.set()
        consumer.join(2)
        self.assertEqual(consumed[0].disposition, CONSUME_DELIVERED)
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(
                conn.execute(
                    f"SELECT COUNT(*) FROM {REPLACEMENT_TRANSACTION_EFFECT_FENCE_TABLE}"
                ).fetchone(),
                (0,),
            )

    def test_fresh_v1_only_writer_refuses_protocol_v2_before_mutation(self):
        """The component stamp remains the admission guard for a fresh old process."""

        def frozen_v1_only_write() -> None:
            with sqlite3.connect(self.store.path) as legacy:
                version = legacy.execute(
                    "SELECT typeof(schema_version), schema_version "
                    "FROM state_schema_components WHERE component=?",
                    (REPLACEMENT_TRANSACTION_COMPONENT,),
                ).fetchone()
                if version != ("integer", 1):
                    raise RuntimeError("legacy writer does not understand this protocol")
                legacy.execute(
                    "UPDATE replacement_transactions SET phase=? "
                    "WHERE workspace_id=? AND action_id=?",
                    (PHASE_COMPLETED, *self.transaction_key.as_row()),
                )

        with self.assertRaisesRegex(RuntimeError, "does not understand"):
            frozen_v1_only_write()

    def test_zero_send_after_lease_expiry_does_not_stale_release(self):
        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET lease_expires_at=? "
                "WHERE workspace_id=? AND action_id=?",
                (NEAR_EXPIRY, *self.transaction_key.as_row()),
            )
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        current = [NOW]

        def zero_send():
            current[0] = AFTER_EXPIRY
            return "zero"

        consumed = self.outbox.consume_reserved(
            self.send_key,
            reservation.token,
            holder=HOLDER,
            clock=lambda: current[0],
            authority_fn=lambda: True,
            send_fn=zero_send,
            send_ok="ok",
            send_zero="zero",
        )

        self.assertEqual(consumed.disposition, RESERVE_LEASE_LOST)
        self.assertEqual(self.outbox.state_of(self.send_key), OUTBOX_RESERVED)
        self.assertEqual(
            self.store.get(self.transaction_key).phase, PHASE_DRAINING_CONTINUATION
        )

    def test_zero_send_db_wait_uses_post_lock_clock(self):
        """R13-F1: BEGIN IMMEDIATE precedes the zero-send lease timestamp."""

        with sqlite3.connect(self.store.path) as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET lease_expires_at=? "
                "WHERE workspace_id=? AND action_id=?",
                (NEAR_EXPIRY, *self.transaction_key.as_row()),
            )
        reservation = self.outbox.reserve(self.send_key, holder=HOLDER, now=NOW)
        original_connect = self.outbox._connect
        connect_calls = []
        current = [NOW]

        def observed_connect():
            connect_calls.append("connect")
            if len(connect_calls) == 2:
                current[0] = AFTER_EXPIRY
            return original_connect()

        self.outbox._connect = observed_connect
        consumed = self.outbox.consume_reserved(
            self.send_key,
            reservation.token,
            holder=HOLDER,
            clock=lambda: current[0],
            authority_fn=lambda: True,
            send_fn=lambda: "zero",
            send_ok="ok",
            send_zero="zero",
        )

        self.assertEqual(consumed.disposition, RESERVE_LEASE_LOST)
        self.assertEqual(self.outbox.state_of(self.send_key), OUTBOX_RESERVED)
        self.assertEqual(
            self.store.get(self.transaction_key).phase, PHASE_DRAINING_CONTINUATION
        )

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
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET phase=? WHERE workspace_id=? "
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
