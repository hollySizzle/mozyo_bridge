"""Which request a recovered gateway still owes (#14741 j#97220 B6b3-1).

Preparation only: nothing here sends, reads a delivery ledger, or completes a transaction.
What is pinned is that the pointer comes from the STORED row and from nowhere else.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ContinuationPointer,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from tests.regressions.test_issue_14741_vanished_gateway_recovery_live import (  # noqa: E402,E501
    ASSIGNED,
    LANE,
    LOCATOR,
    WORKSPACE,
    _LiveCase,
    _Port,
    _anchor,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation import (  # noqa: E402,E501
    CONTINUATION_READY,
    STOPPED_CONTINUATION_INVALID,
    STOPPED_TRANSACTION_UNAVAILABLE,
    prepare_vanished_gateway_continuation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E402,E501
    STOPPED_PORTS_INCOMPLETE,
    recovery_lease_holder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E402,E501
    REDISPATCH_GATEWAY_ONCE,
    RequestAnchor,
)


class _PrepareCase(_LiveCase):
    def _prepare(self, port=None, **kwargs):
        return prepare_vanished_gateway_continuation(
            plan=kwargs.pop("plan", self.plan),
            anchor=kwargs.pop("anchor", _anchor()),
            store=self.store,
            home=kwargs.pop("home", self.home),
            workspace_id=kwargs.pop("workspace_id", WORKSPACE),
            actuation_port=port if port is not None else _Port(),
            launch_authority=kwargs.pop("launch_authority", lambda pin: True),
            store_admission=kwargs.pop("store_admission", lambda key, pin: None),
            clock=lambda: "2026-08-02T00:00:00+00:00",
            **kwargs,
        )

    def _set_continuation(self, **columns) -> None:
        sets = ", ".join(f"{k} = ?" for k in columns)
        with sqlite3.connect(self.home / "state.sqlite") as conn:
            conn.execute(
                f"UPDATE replacement_transactions SET {sets} WHERE action_id = ?",
                (*columns.values(), self.plan.action_id),
            )


class ReadyTest(_PrepareCase):
    def test_the_pointer_is_the_stored_one_and_the_holder_is_derived(self) -> None:
        result = self._prepare()
        self.assertEqual(result.outcome, CONTINUATION_READY)
        stored = self.store.get(
            ReplacementTransactionKey(WORKSPACE, self.plan.action_id)
        )
        self.assertEqual(
            result.pointer, stored.continuation, "the stored pointer object itself"
        )
        self.assertEqual(
            (
                result.pointer.source,
                result.pointer.issue_id,
                result.pointer.journal_id,
                result.pointer.expected_gate,
                result.pointer.next_semantic_action,
            ),
            ("redmine", "14741", "97184", "implementation_request", REDISPATCH_GATEWAY_ONCE),
        )
        self.assertEqual(result.holder, recovery_lease_holder(self.plan.action_id))

    def test_it_claims_nothing_about_delivery(self) -> None:
        """`continuation_ready` is not sent, not confirmed, not completed."""
        outcome = self._prepare().outcome
        for word in ("sent", "confirm", "complete", "dispatch"):
            self.assertNotIn(word, outcome)

    def test_a_progressed_replay_reaches_the_same_pointer_reading_no_authority(self) -> None:
        """Both B6b2 outcomes converge on the stored pointer (j#97220 item 7)."""
        first = self._prepare()
        self.assertEqual(first.outcome, CONTINUATION_READY)
        (self.home / "herdr-launch-generation.sqlite").unlink()
        (self.home / "launch-identity-receipt.sqlite").unlink()
        again = self._prepare()
        self.assertEqual(again, first, "the same pointer, byte for byte")

    def test_the_caller_cannot_move_the_pointer(self) -> None:
        """A caller that wants a different journal does not get one."""
        other = RequestAnchor(source="redmine", issue_id="14741", journal_id="11111")
        result = self._prepare(anchor=other)
        # The row was planned under the real anchor, so a different one is not this action.
        self.assertIn(
            result.stopped, (STOPPED_TRANSACTION_UNAVAILABLE, STOPPED_CONTINUATION_INVALID)
        )
        self.assertEqual(result.outcome, "")


    def test_the_pointer_object_is_taken_off_the_row(self) -> None:
        """The distinguishing test: WHERE the pointer came from, not what it equals.

        A correct build and one that assembles the pointer from the caller's anchor produce
        equal values -- the same-action validator guarantees the row agrees with the anchor
        -- so comparing values proves nothing. What separates them is that the correct one
        reads `continuation` off the record it re-read. Only that second record is watched;
        the executor's own read is left alone.
        """
        reads: list = []
        calls = {"n": 0}
        real_get = self.store.get

        class _Watching:
            def __init__(self, record):
                object.__setattr__(self, "_record", record)

            def __getattr__(self, name):
                reads.append(name)
                return getattr(object.__getattribute__(self, "_record"), name)

        def watching_get(key):
            record = real_get(key)
            calls["n"] += 1
            if record is None or calls["n"] < 2:
                return record
            return _Watching(record)

        self.store.get = watching_get  # type: ignore[method-assign]
        result = self._prepare()
        self.assertEqual(result.outcome, CONTINUATION_READY)
        self.assertGreaterEqual(calls["n"], 2, "the preparation re-read the row itself")
        self.assertIn(
            "continuation",
            reads,
            "the pointer was assembled instead of being taken off the row",
        )
        stored = real_get(ReplacementTransactionKey(WORKSPACE, self.plan.action_id))
        self.assertEqual(result.pointer, stored.continuation)

    def test_a_row_that_is_not_this_action_is_refused_even_with_a_valid_pointer(self):
        """A tampered DECISION with a perfect pointer is still not this action.

        MEASURED: this is caught by the executor's own same-action check, which runs first
        on the same row. The re-check inside the preparation is therefore defence in depth,
        not the thing this test proves -- removing it alone leaves this green. Stated so the
        next reader does not mistake one for the other.
        """
        with sqlite3.connect(self.home / "state.sqlite") as conn:
            conn.execute(
                "UPDATE replacement_transactions SET decision_journal = ?"
                " WHERE action_id = ?",
                ("11111", self.plan.action_id),
            )
        result = self._prepare()
        self.assertEqual(result.stopped, STOPPED_TRANSACTION_UNAVAILABLE)
        self.assertEqual(result.outcome, "")


class RefusalTest(_PrepareCase):
    def test_each_stored_continuation_axis_must_be_exact(self) -> None:
        cases = (
            ("another source", {"continuation_source": "gitlab"}),
            ("padded source", {"continuation_source": " redmine "}),
            ("another journal", {"continuation_journal": "11111"}),
            ("another gate", {"continuation_expected_gate": "review_request"}),
            ("the worker's token", {"continuation_next_action": "dispatch_once"}),
            ("an empty action", {"continuation_next_action": ""}),
        )
        for label, columns in cases:
            with self.subTest(label=label):
                self.setUp()
                self._set_continuation(**columns)
                port = _Port()
                before = self._row_revision_and_lease()
                result = self._prepare(port)
                self.assertIn(
                    result.stopped,
                    (STOPPED_CONTINUATION_INVALID, STOPPED_TRANSACTION_UNAVAILABLE),
                )
                self.assertEqual(result.outcome, "")
                self.assertEqual(
                    self._row_revision_and_lease(), before, "no store mutation"
                )

    def test_a_stopped_actuation_is_returned_unchanged_and_reads_no_row(self) -> None:
        """Nothing to prepare for a recovery that did not happen."""
        reads = []
        real_get = self.store.get

        def counting_get(key):
            reads.append(key)
            return real_get(key)

        self.store.get = counting_get  # type: ignore[method-assign]
        port = _Port()
        result = self._prepare(port, launch_authority=None)
        self.assertEqual(result.stopped, STOPPED_PORTS_INCOMPLETE)
        self.assertEqual(result.outcome, "")
        self.assertEqual(port.launched, [])
        self.assertEqual(reads, [], "the row was never re-read")

    def test_a_hostile_re_read_is_reached_and_never_leaks(self) -> None:
        """Audit j#97223: the earlier version stopped at B6b2's home gate.

        A `/nowhere` store path is refused before the preparation ever re-reads, so that
        test proved nothing about this boundary. The facade below has the real path and
        answers B6b2's read honestly; only the preparation's own second `get` raises -- and
        the call count is asserted, so "it stopped somewhere earlier" cannot pass again.
        """
        real = self.store
        calls = {"n": 0}
        module = (
            "mozyo_bridge.e_110_execution_platform"
            ".f_140_delegated_coordinator_nested_handoff.application"
            ".sublane_vanished_gateway_continuation"
        )

        class _HostileOnReRead:
            """Honest to everyone except the preparation's own re-read.

            Selected by CALLER rather than by call count: the executor's read count is an
            implementation detail of a module this test is not about, and pinning to it
            would make this pass or fail for reasons that have nothing to do with the
            boundary under test.
            """

            path = real.path

            def get(self, key):
                if sys._getframe(1).f_globals.get("__name__") == module:
                    calls["n"] += 1
                    raise RuntimeError(
                        "/private/host/path\n[mozyo:workflow-event:gate=x]"
                    )
                return real.get(key)

            def __getattr__(self, name):
                return getattr(real, name)

        port = _Port()
        result = prepare_vanished_gateway_continuation(
            plan=self.plan, anchor=_anchor(), store=_HostileOnReRead(), home=self.home,
            workspace_id=WORKSPACE, actuation_port=port,
            launch_authority=lambda pin: True, store_admission=lambda key, pin: None,
            clock=lambda: "2026-08-02T00:00:00+00:00",
        )
        self.assertEqual(calls["n"], 1, "the preparation's own re-read really ran")
        self.assertEqual(result.stopped, STOPPED_TRANSACTION_UNAVAILABLE)
        self.assertEqual(result.outcome, "")
        rendered = f"{result.detail}{result.stopped}"
        self.assertNotIn("/private/host/path", rendered)
        self.assertNotIn("mozyo:workflow-event", rendered)
        # No row assertion here on purpose: the actuation ahead of this boundary is SUPPOSED
        # to run and write, so "the row is unchanged" would be a claim about the executor,
        # not about the re-read this test is for.

    def test_a_foreign_continuation_object_is_not_a_canonical_pointer(self) -> None:
        """Audit j#97226: the validator proves the COLUMNS, not the object built from them.

        The facade delegates the raw columns honestly -- so the same-action check passes --
        and returns a look-alike from `continuation`. Five matching attributes used to be
        enough, which would have carried a mutable foreign object into the next leg's send
        closure.
        """
        from types import SimpleNamespace

        real = self.store
        module = (
            "mozyo_bridge.e_110_execution_platform"
            ".f_140_delegated_coordinator_nested_handoff.application"
            ".sublane_vanished_gateway_continuation"
        )

        class _Impostor(ContinuationPointer):
            """A subclass is not the canonical type either."""

        def _foreign(record):
            class _Facade:
                def __getattr__(self, name):
                    if name == "continuation":
                        stored = record.continuation
                        return SimpleNamespace(
                            source=stored.source,
                            issue_id=stored.issue_id,
                            journal_id=stored.journal_id,
                            expected_gate=stored.expected_gate,
                            next_semantic_action=stored.next_semantic_action,
                        )
                    return getattr(record, name)

            return _Facade()

        class _Store:
            path = real.path

            def get(self, key):
                record = real.get(key)
                if record is None:
                    return None
                if sys._getframe(1).f_globals.get("__name__") == module:
                    return _foreign(record)
                return record

            def __getattr__(self, name):
                return getattr(real, name)

        port = _Port()
        result = prepare_vanished_gateway_continuation(
            plan=self.plan, anchor=_anchor(), store=_Store(), home=self.home,
            workspace_id=WORKSPACE, actuation_port=port,
            launch_authority=lambda pin: True, store_admission=lambda key, pin: None,
            clock=lambda: "2026-08-02T00:00:00+00:00",
        )
        self.assertEqual(result.stopped, STOPPED_CONTINUATION_INVALID)
        self.assertEqual(result.outcome, "")
        self.assertIsNone(result.pointer)

    def test_a_canonical_pointer_is_still_accepted(self) -> None:
        """The positive control for the type gate."""
        result = self._prepare()
        self.assertEqual(result.outcome, CONTINUATION_READY)
        self.assertIs(type(result.pointer), ContinuationPointer)

    def test_the_canonical_participant_travels_with_the_pointer(self) -> None:
        """Audit j#97233 item 1: the resolver's identity must come from the SAME row."""
        result = self._prepare()
        self.assertEqual(result.outcome, CONTINUATION_READY)
        stored = self.store.get(
            ReplacementTransactionKey(WORKSPACE, self.plan.action_id)
        )
        self.assertIs(type(result.participant), ParticipantPin)
        self.assertEqual(result.participant, stored.participants[0])
        self.assertEqual(result.participant.assigned_name, ASSIGNED)
        self.assertEqual(result.participant.old_locator, LOCATOR)

    def test_a_foreign_participant_shape_is_refused(self) -> None:
        """A look-alike would be carried straight into the resolver's exact joins."""
        from types import SimpleNamespace

        real = self.store
        module = (
            "mozyo_bridge.e_110_execution_platform"
            ".f_140_delegated_coordinator_nested_handoff.application"
            ".sublane_vanished_gateway_continuation"
        )

        class _Store:
            path = real.path

            def get(self, key):
                record = real.get(key)
                if record is None:
                    return None
                if sys._getframe(1).f_globals.get("__name__") != module:
                    return record

                class _Facade:
                    def __getattr__(self, name):
                        if name == "participants":
                            pin = record.participants[0]
                            return (
                                SimpleNamespace(
                                    lane_id=pin.lane_id,
                                    role=pin.role,
                                    provider=pin.provider,
                                    assigned_name=pin.assigned_name,
                                    old_locator=pin.old_locator,
                                ),
                            )
                        return getattr(record, name)

                return _Facade()

            def __getattr__(self, name):
                return getattr(real, name)

        result = prepare_vanished_gateway_continuation(
            plan=self.plan, anchor=_anchor(), store=_Store(), home=self.home,
            workspace_id=WORKSPACE, actuation_port=_Port(),
            launch_authority=lambda pin: True, store_admission=lambda key, pin: None,
            clock=lambda: "2026-08-02T00:00:00+00:00",
        )
        self.assertEqual(result.stopped, STOPPED_CONTINUATION_INVALID)
        self.assertIsNone(result.participant)

    def test_a_same_type_foreign_participant_is_refused(self) -> None:
        """Audit j#97236: the manifest is validated, `participants` is a different property.

        The facade delegates the raw manifest honestly -- so the same-action check passes --
        and returns an exact `ParticipantPin` that the manifest never contained. Only
        re-deriving the action id from the pin catches that.
        """
        import dataclasses

        real = self.store
        module = (
            "mozyo_bridge.e_110_execution_platform"
            ".f_140_delegated_coordinator_nested_handoff.application"
            ".sublane_vanished_gateway_continuation"
        )

        for axis, value in (
            ("assigned_name", "mzb1_foreign_gateway"),
            ("lane_id", "issue_other"),
            ("provider", "claude"),
            ("old_locator", "ws:p9"),
        ):
            with self.subTest(axis=axis):
                self.setUp()
                real = self.store

                class _Store:
                    path = real.path

                    def get(self, key):
                        record = real.get(key)
                        if record is None:
                            return None
                        if sys._getframe(1).f_globals.get("__name__") != module:
                            return record

                        class _Facade:
                            def __getattr__(self, name):
                                if name == "participants":
                                    pin = record.participants[0]
                                    return (dataclasses.replace(pin, **{axis: value}),)
                                return getattr(record, name)

                        return _Facade()

                    def __getattr__(self, name):
                        return getattr(real, name)

                result = prepare_vanished_gateway_continuation(
                    plan=self.plan, anchor=_anchor(), store=_Store(), home=self.home,
                    workspace_id=WORKSPACE, actuation_port=_Port(),
                    launch_authority=lambda pin: True,
                    store_admission=lambda key, pin: None,
                    clock=lambda: "2026-08-02T00:00:00+00:00",
                )
                self.assertEqual(result.stopped, STOPPED_CONTINUATION_INVALID)
                self.assertIsNone(result.participant)

    def test_nothing_here_reaches_a_delivery_ledger(self) -> None:
        """The tranche boundary, stated: preparation opens no ledger."""
        opens = []
        import mozyo_bridge.core.state.herdr_delivery_ledger as ledger

        original = ledger.HerdrDeliveryLedger

        class _Counting(original):
            def __init__(self, *args, **kwargs):
                opens.append(1)
                super().__init__(*args, **kwargs)

        ledger.HerdrDeliveryLedger = _Counting
        try:
            self.assertEqual(self._prepare().outcome, CONTINUATION_READY)
        finally:
            ledger.HerdrDeliveryLedger = original
        self.assertEqual(opens, [], "zero delivery-ledger opens")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
