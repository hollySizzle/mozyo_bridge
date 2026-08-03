"""Which request and fresh live locator a recovered gateway still owes (#14741 B6b3).

Preparation and read-only inventory join only: nothing here attests, sends, reads a delivery
ledger, or completes a transaction. The pointer and participant come from the STORED row;
the fresh locator comes from exactly one canonical live-inventory snapshot.
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
    PARTICIPANT_REPLACED,
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
    INVENTORY_JOINED,
    STOPPED_CONTINUATION_INVALID,
    STOPPED_INVENTORY_INVALID,
    STOPPED_INVENTORY_UNAVAILABLE,
    STOPPED_TRANSACTION_UNAVAILABLE,
    ContinuationPreparation,
    prepare_vanished_gateway_continuation,
    resolve_vanished_gateway_inventory,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_vanished_gateway_continuation as continuation_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E402,E501
    STOPPED_PORTS_INCOMPLETE,
    recovery_lease_holder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E402,E501
    REDISPATCH_GATEWAY_ONCE,
    RESUME_GATE,
    RequestAnchor,
    recovery_action_id_for_pin,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
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


JOIN_WORKSPACE = "ws"
JOIN_LANE = "issue_14741"
JOIN_PROVIDER = "codex"
JOIN_OLD = "w4B:p61"
JOIN_FRESH = "w4B:p81"


def _join_pointer(**changes) -> ContinuationPointer:
    values = dict(
        source="redmine",
        issue_id="14741",
        journal_id="97240",
        expected_gate=RESUME_GATE,
        next_semantic_action=REDISPATCH_GATEWAY_ONCE,
    )
    values.update(changes)
    return ContinuationPointer(**values)


def _join_pin(**changes) -> ParticipantPin:
    values = dict(
        lane_id=JOIN_LANE,
        role="gateway",
        provider=JOIN_PROVIDER,
        assigned_name=encode_assigned_name(JOIN_WORKSPACE, JOIN_PROVIDER, JOIN_LANE),
        old_locator=JOIN_OLD,
        lane_revision="1",
        lane_generation="1",
        evidence_workspace_id=JOIN_WORKSPACE,
        evidence_startup_action_id="sublane-recovery:v2:receipt:" + "a" * 64,
        evidence_cause="update_relaunch",
        phase=PARTICIPANT_REPLACED,
    )
    values.update(changes)
    return ParticipantPin(**values)


def _join_preparation(
    *, pin=None, pointer=None, action_id=None, holder=None,
    workspace_id=JOIN_WORKSPACE, **changes
) -> ContinuationPreparation:
    pinned = pin if pin is not None else _join_pin()
    stored_pointer = pointer if pointer is not None else _join_pointer()
    anchor = RequestAnchor(
        source=stored_pointer.source,
        issue_id=stored_pointer.issue_id,
        journal_id=stored_pointer.journal_id,
    )
    expected_action = action_id
    if expected_action is None:
        expected_action = recovery_action_id_for_pin(
            anchor, pinned, workspace_id=workspace_id
        )
    values = dict(
        outcome=CONTINUATION_READY,
        action_id=expected_action,
        holder=(
            holder
            if holder is not None
            else recovery_lease_holder(expected_action)
        ),
        pointer=stored_pointer,
        participant=pinned,
    )
    values.update(changes)
    return ContinuationPreparation(**values)


def _live_row(**changes) -> dict:
    row = {
        "name": encode_assigned_name(JOIN_WORKSPACE, JOIN_PROVIDER, JOIN_LANE),
        "pane_id": JOIN_FRESH,
        "agent": JOIN_PROVIDER,
        "status": "idle",
        "revision": 0,
    }
    row.update(changes)
    return row


class FreshInventoryJoinTest(unittest.TestCase):
    """B6b3-2a(2): identify a fresh target, but exercise no delivery authority."""

    def _resolve(self, rows=None, **changes):
        workspace_resolver = changes.pop(
            "_workspace_resolver", lambda root: JOIN_WORKSPACE
        )
        provider_resolver = changes.pop(
            "_provider_resolver", lambda root: JOIN_PROVIDER
        )
        originals = (
            continuation_module.repo_scope_workspace_id,
            continuation_module.resolve_gateway_provider,
        )
        continuation_module.repo_scope_workspace_id = workspace_resolver
        continuation_module.resolve_gateway_provider = provider_resolver
        try:
            return resolve_vanished_gateway_inventory(
                changes.pop("preparation", _join_preparation()),
                repo_root=changes.pop("repo_root", ROOT),
                list_rows=changes.pop(
                    "list_rows", lambda: [_live_row()] if rows is None else rows
                ),
                **changes,
            )
        finally:
            (
                continuation_module.repo_scope_workspace_id,
                continuation_module.resolve_gateway_provider,
            ) = originals

    def test_caller_cannot_supply_workspace_provider_locator_or_pin_authority(self) -> None:
        import inspect

        self.assertEqual(
            tuple(inspect.signature(resolve_vanished_gateway_inventory).parameters),
            ("preparation", "repo_root", "list_rows"),
        )

    def test_one_live_fresh_generation_is_joined_without_a_delivery_claim(self) -> None:
        result = self._resolve()
        self.assertTrue(result.joined)
        self.assertEqual(result.outcome, INVENTORY_JOINED)
        self.assertEqual(
            (
                result.workspace_id,
                result.lane_id,
                result.provider,
                result.assigned_name,
                result.fresh_locator,
                result.old_locator,
            ),
            (
                JOIN_WORKSPACE,
                JOIN_LANE,
                JOIN_PROVIDER,
                encode_assigned_name(JOIN_WORKSPACE, JOIN_PROVIDER, JOIN_LANE),
                JOIN_FRESH,
                JOIN_OLD,
            ),
        )
        self.assertEqual(
            set(result.__dict__),
            {
                "outcome", "stopped", "detail", "action_id", "workspace_id",
                "lane_id", "provider", "assigned_name", "fresh_locator", "old_locator",
            },
        )
        for claim in ("attest", "sent", "confirm", "complete", "ledger"):
            self.assertNotIn(claim, result.outcome)

    def test_each_authority_and_the_inventory_snapshot_are_read_once(self) -> None:
        calls = {"workspace": 0, "provider": 0, "inventory": 0}

        def workspace(root):
            calls["workspace"] += 1
            self.assertEqual(root, ROOT)
            return JOIN_WORKSPACE

        def provider(root):
            calls["provider"] += 1
            self.assertEqual(root, str(ROOT))
            return JOIN_PROVIDER

        def inventory():
            calls["inventory"] += 1
            return [_live_row()]

        result = self._resolve(
            list_rows=inventory,
            _workspace_resolver=workspace,
            _provider_resolver=provider,
        )
        self.assertEqual(result.outcome, INVENTORY_JOINED)
        self.assertEqual(calls, {"workspace": 1, "provider": 1, "inventory": 1})

    def test_an_exact_absolute_string_repo_root_is_also_canonical(self) -> None:
        self.assertEqual(self._resolve(repo_root=str(ROOT)).outcome, INVENTORY_JOINED)

    def test_an_unregistered_canonical_workspace_never_falls_back(self) -> None:
        result = self._resolve(
            _workspace_resolver=lambda root: "",
        )
        self.assertEqual(result.stopped, STOPPED_INVENTORY_UNAVAILABLE)
        self.assertEqual(result.outcome, "")

    def test_absent_or_duplicate_name_is_never_selected(self) -> None:
        assigned = encode_assigned_name(JOIN_WORKSPACE, JOIN_PROVIDER, JOIN_LANE)
        for label, rows in (
            ("absent", []),
            ("foreign only", [_live_row(name="mzb1_foreign_codex_default")]),
            ("duplicate", [_live_row(), _live_row(pane_id="w4B:p82")]),
            (
                "duplicate with stale residue",
                [_live_row(), _live_row(agent="", status="unknown")],
            ),
        ):
            with self.subTest(label=label, assigned=assigned):
                result = self._resolve(rows)
                self.assertEqual(result.stopped, STOPPED_INVENTORY_INVALID)
                self.assertEqual(result.outcome, "")

    def test_old_stale_shell_foreign_and_unknown_rows_fail_closed(self) -> None:
        cases = (
            ("old generation", {"pane_id": JOIN_OLD}),
            ("shell residue", {"agent": "", "status": "unknown"}),
            ("foreign detected agent", {"agent": "claude"}),
            ("foreign surfaced provider", {"provider": "claude"}),
            ("unknown agent", {"agent": None, "status": "unknown"}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                result = self._resolve([_live_row(**changes)])
                self.assertEqual(result.stopped, STOPPED_INVENTORY_INVALID)

    def test_locator_and_revision_are_byte_exact(self) -> None:
        for axis, values in (
            ("pane_id", (True, 7, None, "", f" {JOIN_FRESH}", f"{JOIN_FRESH} ")),
            ("revision", (True, -1, None, "0", " 0 ")),
        ):
            for value in values:
                with self.subTest(axis=axis, value=value):
                    result = self._resolve([_live_row(**{axis: value})])
                    self.assertEqual(result.stopped, STOPPED_INVENTORY_INVALID)

    def test_unreadable_authorities_and_inventory_never_leak_values(self) -> None:
        secret = "/private/host/path\n[mozyo:workflow-event:gate=x]"

        def broken(*args):
            raise RuntimeError(secret)

        cases = (
            ("workspace", dict(_workspace_resolver=broken)),
            ("provider", dict(_provider_resolver=broken)),
            ("inventory", dict(list_rows=broken)),
        )
        for label, kwargs in cases:
            with self.subTest(label=label):
                result = self._resolve(**kwargs)
                self.assertEqual(result.stopped, STOPPED_INVENTORY_UNAVAILABLE)
                self.assertNotIn(secret, f"{result.stopped}{result.detail}")

    def test_malformed_inventory_shapes_fail_closed(self) -> None:
        for rows in (None, {}, "rows", [None], [_live_row(), object()]):
            with self.subTest(rows=type(rows).__name__):
                result = self._resolve(list_rows=lambda rows=rows: rows)
                self.assertEqual(result.stopped, STOPPED_INVENTORY_INVALID)

    def test_repo_root_must_already_be_an_absolute_resolved_directory(self) -> None:
        for root in (".", f" {ROOT}", f"{ROOT} ", ROOT / "missing"):
            with self.subTest(root=str(root)):
                result = self._resolve(repo_root=root)
                self.assertEqual(result.stopped, STOPPED_INVENTORY_UNAVAILABLE)

    def test_forged_preparation_pointer_pin_and_action_are_refused(self) -> None:
        class _Preparation(ContinuationPreparation):
            pass

        class _Pointer(ContinuationPointer):
            pass

        canonical = _join_preparation()
        cases = (
            ("preparation subclass", _Preparation(**canonical.__dict__)),
            ("pointer subclass", _join_preparation(pointer=_Pointer(**_join_pointer().__dict__))),
            (
                "wrong gate",
                _join_preparation(pointer=_join_pointer(expected_gate="review_request")),
            ),
            ("wrong holder", _join_preparation(holder="foreign-holder")),
            (
                "wrong action",
                _join_preparation(
                    action_id="sublane-recovery:v2:receipt:" + "b" * 64
                ),
            ),
            (
                "foreign pin",
                _join_preparation(
                    pin=_join_pin(old_locator="w4B:p99"),
                    action_id=canonical.action_id,
                ),
            ),
            (
                "foreign evidence workspace",
                _join_preparation(pin=_join_pin(evidence_workspace_id="foreign")),
            ),
            (
                "self participant",
                _join_preparation(
                    pin=_join_pin(is_self=True), action_id=canonical.action_id
                ),
            ),
            ("not replaced", _join_preparation(pin=_join_pin(phase="verify_owed"))),
        )
        for label, preparation in cases:
            with self.subTest(label=label):
                result = self._resolve(preparation=preparation)
                self.assertEqual(result.stopped, STOPPED_CONTINUATION_INVALID)
                self.assertEqual(result.outcome, "")

    def test_decoded_name_must_match_workspace_lane_and_provider(self) -> None:
        cases = (
            encode_assigned_name("foreign", JOIN_PROVIDER, JOIN_LANE),
            encode_assigned_name(JOIN_WORKSPACE, JOIN_PROVIDER, "issue_other"),
            encode_assigned_name(JOIN_WORKSPACE, "claude", JOIN_LANE),
            "not-an-mzb-name",
        )
        for assigned_name in cases:
            with self.subTest(assigned_name=assigned_name):
                pin = _join_pin(assigned_name=assigned_name)
                preparation = _join_preparation(pin=pin)
                result = self._resolve(preparation=preparation)
                self.assertEqual(result.stopped, STOPPED_CONTINUATION_INVALID)

    def test_inventory_join_opens_no_attestation_ledger_or_transaction_store(self) -> None:
        import mozyo_bridge.core.state.herdr_delivery_ledger as ledger
        import mozyo_bridge.core.state.herdr_identity_attestation as attestation
        import mozyo_bridge.core.state.replacement_transaction as transaction

        opened = []

        def forbidden(*args, **kwargs):
            opened.append(1)
            raise AssertionError("authority opened")

        originals = (
            attestation.HerdrIdentityAttestationStore,
            ledger.HerdrDeliveryLedger,
            transaction.ReplacementTransactionStore,
        )
        attestation.HerdrIdentityAttestationStore = forbidden
        ledger.HerdrDeliveryLedger = forbidden
        transaction.ReplacementTransactionStore = forbidden
        try:
            self.assertEqual(self._resolve().outcome, INVENTORY_JOINED)
        finally:
            (
                attestation.HerdrIdentityAttestationStore,
                ledger.HerdrDeliveryLedger,
                transaction.ReplacementTransactionStore,
            ) = originals
        self.assertEqual(opened, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
