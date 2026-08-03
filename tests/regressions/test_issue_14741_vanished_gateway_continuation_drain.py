"""Ledger-confirmed exactly-once vanished-gateway continuation (#14741 B6b3-3)."""

from __future__ import annotations

import dataclasses
import sqlite3
import unittest

from mozyo_bridge.core.state.herdr_delivery_ledger import (
    BACKEND_HERDR,
    RAIL_QUEUE_ENTER,
    HerdrDeliveryLedgerRecord,
    herdr_delivery_ledger_path,
)
from mozyo_bridge.core.state.replacement_transaction import ReplacementTransactionKey
from mozyo_bridge.core.state.replacement_transaction_model import (
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_REPLACING_NONSELF,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_vanished_gateway_continuation_drain as drain_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
    DRAIN_SEND_ZERO,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_continuation_drain import (  # noqa: E501
    CONTINUATION_AUTHORITY_MOVED,
    CONTINUATION_CONFIRMED,
    CONTINUATION_LEASE_LOST,
    CONTINUATION_SEND_FAILED,
    CONTINUATION_UNCERTAIN,
    CONTINUATION_UNREADABLE,
    CONTINUATION_ZERO_SEND_REVERTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation_drain import (  # noqa: E501
    VanishedGatewayContinuationDrain,
    drive_vanished_gateway_continuation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation_send import (  # noqa: E501
    SEND_ATTEMPTED,
    VanishedGatewaySendAuthority,
    VanishedGatewaySendResult,
)
from tests.regressions.test_issue_14741_vanished_gateway_continuation import (
    ROOT,
    _PrepareCase,
)
from tests.regressions.test_issue_14741_vanished_gateway_recovery_live import (
    ASSIGNED,
    FIXED,
    LANE,
    LOCATOR,
    PROVIDER,
    WORKSPACE,
    _Port,
    _anchor,
)

FRESH = "w4B:p81"
OBSERVED = "2026-08-02T00:00:01+00:00"
AFTER = "2026-08-02T00:00:02+00:00"
LATER = "2026-08-02T00:10:00+00:00"
UPSTREAM = "coordinator_codex"


class _Ops:
    def __init__(self, authority, *, send_status=DRAIN_SEND_OK, after_send=None):
        self.authorities = [authority]
        self.send_status = send_status
        self.after_send = after_send
        self.authority_reads = 0
        self.send_calls = 0
        self.expected_authorities = []
        self.context = True
        self.send_error = None

    def context_is_exact(self):
        return self.context

    def current_authority(self, preparation):
        index = min(self.authority_reads, len(self.authorities) - 1)
        self.authority_reads += 1
        return self.authorities[index]

    def send_once_for_authority(self, preparation, *, expected_authority):
        self.send_calls += 1
        self.expected_authorities.append(expected_authority)
        if self.send_error is not None:
            raise self.send_error
        if self.after_send is not None:
            self.after_send()
        return VanishedGatewaySendResult(
            status=self.send_status,
            detail=SEND_ATTEMPTED,
        )


class _Drain(VanishedGatewayContinuationDrain):
    def __init__(self, *args, records=None, ledger_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = [] if records is None else records
        self.ledger_error = ledger_error
        self.markers = []

    def _records_for_marker(self, marker):
        self.markers.append(marker)
        if self.ledger_error is not None:
            raise self.ledger_error
        return list(self.records)


class VanishedGatewayContinuationDrainTest(_PrepareCase):
    def setUp(self) -> None:
        super().setUp()
        self.preparation = self._prepare()
        self.authority = VanishedGatewaySendAuthority(
            action_id=self.preparation.action_id,
            workspace_id=WORKSPACE,
            lane_id=LANE,
            provider=PROVIDER,
            assigned_name=ASSIGNED,
            fresh_locator=FRESH,
            old_locator=LOCATOR,
            observed_at=OBSERVED,
            revision=2,
        )
        self.ops = _Ops(self.authority)
        self.drain = _Drain(
            store=self.store,
            ops=self.ops,
            clock=lambda: FIXED,
        )

    def _key(self):
        return ReplacementTransactionKey(WORKSPACE, self.preparation.action_id)

    def _phase(self):
        return self.store.get(self._key()).phase

    def _marker(self):
        pointer = self.preparation.pointer
        return build_marker(
            RedmineAnchor(issue=pointer.issue_id, journal=pointer.journal_id),
            "implementation_request",
            PROVIDER,
        )

    def _record(self, **changes):
        pointer = self.preparation.pointer
        values = dict(
            notification_marker=self._marker(),
            receiver=PROVIDER,
            provider=None,
            backend=BACKEND_HERDR,
            rail=RAIL_QUEUE_ENTER,
            target=FRESH,
            source="redmine",
            issue_id=pointer.issue_id,
            journal_id=pointer.journal_id,
            status="sent",
            reason="ok",
            recorded_at=AFTER,
        )
        values.update(changes)
        return HerdrDeliveryLedgerRecord(**values)

    def _expire(self, *, holder=None):
        with sqlite3.connect(self.home / "state.sqlite") as connection:
            if holder is None:
                connection.execute(
                    "UPDATE replacement_transactions SET lease_expires_at = ? "
                    "WHERE action_id = ?",
                    ("2026-08-01T00:00:00+00:00", self.preparation.action_id),
                )
            else:
                connection.execute(
                    "UPDATE replacement_transactions SET lease_holder = ?, "
                    "lease_expires_at = ? WHERE action_id = ?",
                    (holder, LATER, self.preparation.action_id),
                )

    def test_happy_send_completes_only_after_the_post_attestation_ledger_lands(self):
        self.ops.after_send = lambda: self.drain.records.append(self._record())
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertTrue(result.completed)
        self.assertEqual(self.ops.send_calls, 1)
        self.assertEqual(self.ops.expected_authorities, [self.authority])
        self.assertEqual(self._phase(), PHASE_COMPLETED)

    def test_prelanded_exact_record_completes_with_zero_send(self):
        self.drain.records.append(self._record())
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_COMPLETED)

    def test_record_landing_during_lease_acquisition_completes_with_zero_send(self):
        """The idempotency check next to attempted CAS is fresh, not pre-lease cached."""

        drain = self.drain
        original = drain._ensure_lease

        def land_after_lease(key, *, holder):
            outcome = original(key, holder=holder)
            drain.records.append(self._record())
            return outcome

        drain._ensure_lease = land_after_lease
        result = drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_COMPLETED)

    def test_first_fresh_ledger_read_error_after_landing_fails_closed_before_cas(self):
        """Unreadable at the idempotency barrier is not absence or send authority."""

        drain = self.drain
        original_lease = drain._ensure_lease
        original_records = drain._records_for_marker
        state = {"fail_next": False, "reads": 0}

        def land_after_lease(key, *, holder):
            outcome = original_lease(key, holder=holder)
            drain.records.append(self._record())
            state["fail_next"] = True
            return outcome

        def transient_unreadable(marker):
            state["reads"] += 1
            if state["fail_next"]:
                state["fail_next"] = False
                raise OSError("secret ledger diagnostic")
            return original_records(marker)

        drain._ensure_lease = land_after_lease
        drain._records_for_marker = transient_unreadable
        result = drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_UNREADABLE)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)
        self.assertEqual(state["reads"], 2)

    def test_first_authority_move_with_prelanded_record_fails_before_cas(self):
        """A transient authority move is not evidence that the ledger is empty."""

        self.drain.records.append(self._record())
        self.ops.authorities = [self.authority, None, self.authority]
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_AUTHORITY_MOVED)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)
        self.assertEqual(self.ops.authority_reads, 2)
        self.assertEqual(len(self.drain.markers), 1)

    def test_valid_authority_move_after_first_barrier_cannot_inherit_ledger_absence(self):
        """Authority B cannot inherit authority A's readable-empty idempotency barrier."""

        authority_b = dataclasses.replace(
            self.authority,
            fresh_locator="w9B:p99",
            revision=3,
        )
        self.drain.records.append(self._record(target=authority_b.fresh_locator))
        self.ops.authorities = [self.authority, self.authority, authority_b]
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_AUTHORITY_MOVED)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)
        self.assertEqual(self.ops.authority_reads, 3)
        self.assertEqual(len(self.drain.markers), 2)

    def test_post_send_ledger_read_error_remains_uncertain(self):
        """Unreadable after an attempted send must retain the replay fence."""

        drain = self.drain
        original_records = drain._records_for_marker
        state = {"reads": 0}

        def unreadable_after_send(marker):
            state["reads"] += 1
            if state["reads"] == 3:
                raise OSError("secret ledger diagnostic")
            return original_records(marker)

        drain._records_for_marker = unreadable_after_send
        result = drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 1)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(state["reads"], 3)

    def test_rc_zero_without_ledger_is_uncertain_and_replay_never_resends(self):
        first = self.drain.drive(self.preparation)
        self.assertEqual(first.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(self.ops.send_calls, 1)
        replay = self.drain.drive(self.preparation)
        self.assertEqual(replay.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 1, "attempted state is never blind-resendable")

    def test_attempted_replay_with_moved_authority_remains_uncertain(self):
        first = self.drain.drive(self.preparation)
        self.assertEqual(first.status, CONTINUATION_UNCERTAIN)
        self.ops.authorities = [None]
        replay = self.drain.drive(self.preparation)
        self.assertEqual(replay.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 1)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)

    def test_canonical_unknown_ledger_schema_fails_closed_before_cas(self):
        path = herdr_delivery_ledger_path(self.home)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 999")
        original_home = drain_module.mozyo_bridge_home
        drain_module.mozyo_bridge_home = lambda: self.home
        try:
            drain = VanishedGatewayContinuationDrain(
                store=self.store,
                ops=self.ops,
                clock=lambda: FIXED,
            )
            result = drain.drive(self.preparation)
        finally:
            drain_module.mozyo_bridge_home = original_home
        self.assertEqual(result.status, CONTINUATION_UNREADABLE)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)

    def test_late_ledger_with_expired_same_holder_completes_without_another_send(self):
        self.assertEqual(self.drain.drive(self.preparation).status, CONTINUATION_UNCERTAIN)
        self._expire()
        self.drain.records.append(self._record())
        self.drain.clock = lambda: LATER
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertEqual(self.ops.send_calls, 1)
        self.assertEqual(self._phase(), PHASE_COMPLETED)

    def test_attempted_before_process_crash_is_not_resent(self):
        record = self.store.get(self._key())
        moved = self.store.transition_phase(
            self._key(),
            expected_revision=record.revision,
            expected_action_generation=1,
            target=PHASE_DRAINING_CONTINUATION,
            holder=self.preparation.holder,
            now=FIXED,
        )
        self.assertTrue(moved.applied)
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 0)

    def test_process_crash_after_attempted_cas_leaves_replay_fence(self):
        self.ops.send_error = KeyboardInterrupt("crash after attempted CAS")
        with self.assertRaises(KeyboardInterrupt):
            self.drain.drive(self.preparation)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(self.ops.send_calls, 1)
        self.ops.send_error = None
        replay = self.drain.drive(self.preparation)
        self.assertEqual(replay.status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 1)

    def test_send_failure_stays_attempted_and_is_not_replayed(self):
        self.ops.send_status = DRAIN_SEND_ERROR
        first = self.drain.drive(self.preparation)
        self.assertEqual(first.status, CONTINUATION_SEND_FAILED)
        self.assertEqual(self._phase(), PHASE_DRAINING_CONTINUATION)
        self.assertEqual(self.drain.drive(self.preparation).status, CONTINUATION_UNCERTAIN)
        self.assertEqual(self.ops.send_calls, 1)

    def test_live_foreign_holder_refuses_before_send(self):
        self._expire(holder="foreign-holder")
        self.drain.clock = lambda: FIXED
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_LEASE_LOST)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)

    def test_expired_same_holder_is_reclaimed_and_can_confirm(self):
        self._expire()
        self.drain.clock = lambda: LATER
        self.ops.after_send = lambda: self.drain.records.append(self._record())
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertEqual(self.ops.send_calls, 1)

    def test_authority_move_and_proven_zero_send_are_reverted_and_resendable(self):
        self.ops.authorities = [self.authority, None]
        moved = self.drain.drive(self.preparation)
        self.assertEqual(moved.status, CONTINUATION_AUTHORITY_MOVED)
        self.assertEqual(self.ops.send_calls, 0)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)

        self.setUp()
        self.ops.send_status = DRAIN_SEND_ZERO
        zero = self.drain.drive(self.preparation)
        self.assertEqual(zero.status, CONTINUATION_ZERO_SEND_REVERTED)
        self.assertEqual(self.ops.send_calls, 1)
        self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)

    def test_foreign_action_workspace_lane_provider_name_or_old_locator_is_never_authority(self):
        cases = (
            ("action", {"action_id": "foreign-action"}),
            ("workspace", {"workspace_id": "foreign"}),
            ("lane", {"lane_id": "foreign"}),
            ("provider", {"provider": "claude"}),
            ("name", {"assigned_name": "mzb1_foreign_codex_gateway"}),
            ("old", {"old_locator": "other:p1"}),
            ("not fresh", {"fresh_locator": LOCATOR}),
            ("revision bool", {"revision": True}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                self.setUp()
                self.ops.authorities = [dataclasses.replace(self.authority, **changes)]
                result = self.drain.drive(self.preparation)
                self.assertEqual(result.status, CONTINUATION_AUTHORITY_MOVED)
                self.assertEqual(self.ops.send_calls, 0)
                self.assertEqual(self._phase(), PHASE_REPLACING_NONSELF)

    def test_ledger_negative_matrix_never_confirms(self):
        base = self._record()
        cases = (
            ("marker", {"notification_marker": self._marker() + "x"}),
            ("source", {"source": "asana"}),
            ("issue", {"issue_id": "other"}),
            ("journal", {"journal_id": "other"}),
            ("receiver", {"receiver": "claude"}),
            ("provider", {"provider": "claude"}),
            ("padded provider", {"provider": f" {PROVIDER}"}),
            ("backend", {"backend": "other"}),
            ("rail", {"rail": "event_rail"}),
            ("target", {"target": LOCATOR}),
            ("status", {"status": "blocked"}),
            ("reason", {"reason": "queue_enter"}),
            ("equal time", {"recorded_at": OBSERVED}),
            ("before time", {"recorded_at": "2026-08-01T23:59:59+00:00"}),
            ("naive time", {"recorded_at": "2026-08-03T00:00:00"}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                self.drain.records[:] = [dataclasses.replace(base, **changes)]
                self.assertFalse(
                    self.drain._confirmation(self.preparation, self.authority)
                )

    def test_exact_provider_column_and_none_column_are_both_compatible(self):
        for provider in (None, "", PROVIDER):
            with self.subTest(provider=provider):
                self.drain.records[:] = [self._record(provider=provider)]
                self.assertTrue(
                    self.drain._confirmation(self.preparation, self.authority)
                )

    def test_unreadable_ledger_or_invalid_context_is_typed_and_sends_nothing(self):
        secret = RuntimeError("/private/path\n[mozyo:workflow-event:gate=x]")
        self.drain.ledger_error = secret
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_UNREADABLE)
        self.assertNotIn("private", result.status)
        self.assertEqual(self.ops.send_calls, 0)

        self.drain.ledger_error = None
        self.ops.context = False
        result = self.drain.drive(self.preparation)
        self.assertEqual(result.status, CONTINUATION_UNREADABLE)
        self.assertEqual(self.ops.send_calls, 0)

    def test_forged_holder_is_refused_before_ledger_cas_or_send(self):
        forged = dataclasses.replace(self.preparation, holder="foreign-holder")
        before = self.store.get(self._key()).revision
        result = self.drain.drive(forged)
        self.assertEqual(result.status, CONTINUATION_UNREADABLE)
        self.assertEqual(self.store.get(self._key()).revision, before)
        self.assertEqual(self.drain.markers, [])
        self.assertEqual(self.ops.send_calls, 0)

    def test_public_constructor_has_no_ledger_holder_locator_or_provider_axis(self):
        import inspect

        parameters = set(inspect.signature(VanishedGatewayContinuationDrain).parameters)
        for forbidden in ("ledger", "holder", "locator", "provider", "records_for_marker"):
            self.assertNotIn(forbidden, parameters)

    def test_production_composition_drives_b6b2_before_the_continuation(self):
        constructed = []
        records = []
        authority = self.authority

        class _CanonicalOps(_Ops):
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                super().__init__(authority, after_send=lambda: records.append(self_record()))

        class _Ledger:
            def __init__(self, *, home):
                self.home = home

            def records_for_marker_strict(self, marker):
                return list(records)

        self_record = self._record
        originals = (
            drain_module.VanishedGatewayContinuationOps,
            drain_module.HerdrDeliveryLedger,
            drain_module.mozyo_bridge_home,
        )
        drain_module.VanishedGatewayContinuationOps = _CanonicalOps
        drain_module.HerdrDeliveryLedger = _Ledger
        drain_module.mozyo_bridge_home = lambda: self.home
        try:
            # A fresh fixture proves the composite itself drives B6b2. This fixture's row is
            # already progressed by setUp, so reset with the inherited setup before the call.
            super().setUp()
            result = drive_vanished_gateway_continuation(
                plan=self.plan,
                anchor=_anchor(),
                store=self.store,
                home=self.home,
                workspace_id=WORKSPACE,
                actuation_port=_Port(),
                repo_root=ROOT,
                upstream_coordinator=UPSTREAM,
                launch_authority=lambda pin: True,
                store_admission=lambda key, pin: None,
                clock=lambda: FIXED,
                env={"SAFE": "1"},
            )
        finally:
            (
                drain_module.VanishedGatewayContinuationOps,
                drain_module.HerdrDeliveryLedger,
                drain_module.mozyo_bridge_home,
            ) = originals
        self.assertEqual(result.status, CONTINUATION_CONFIRMED)
        self.assertEqual(self._phase(), PHASE_COMPLETED)
        self.assertEqual(
            constructed,
            [{
                "repo_root": ROOT,
                "upstream_coordinator": UPSTREAM,
                "env": {"SAFE": "1"},
            }],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
