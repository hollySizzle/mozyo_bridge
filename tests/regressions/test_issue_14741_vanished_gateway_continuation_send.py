"""Canonical, action-time rechecked vanished-gateway send edge (#14741 B6b3-2b)."""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_vanished_gateway_continuation as continuation_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_vanished_gateway_continuation_send as send_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
    DRAIN_SEND_ZERO,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_continuation_send import (  # noqa: E501
    SEND_ATTEMPTED,
    SEND_AUTHORITY_INVALID,
    SEND_AUTHORITY_MOVED,
    SEND_FAILED,
    VanishedGatewayContinuationOps,
)
from tests.regressions.test_issue_14741_vanished_gateway_continuation import (
    JOIN_FRESH,
    JOIN_LANE,
    JOIN_OLD,
    JOIN_PROVIDER,
    JOIN_WORKSPACE,
    ROOT,
    _join_preparation,
    _live_row,
)

OBSERVED = "2026-08-03T01:02:03+00:00"
UPSTREAM = "coordinator_codex"
FRESH_TWO = "w4B:p82"


class _Dispatch:
    def __init__(self, *, rc=0, error=None):
        self.rc = rc
        self.error = error
        self.calls = []

    def dispatch_implementation_request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.rc


class _Ops(VanishedGatewayContinuationOps):
    def __init__(self, *args, rows, dispatch, before_read=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._snapshots = list(rows)
        self._dispatch = dispatch
        self._before_read = before_read
        self.reads = 0
        self.builds = 0

    def _rows(self):
        index = min(self.reads, len(self._snapshots) - 1)
        self.reads += 1
        if self._before_read is not None:
            self._before_read(self.reads)
        return self._snapshots[index]

    def _dispatch_ops(self, preparation, root):
        self.builds += 1
        return self._dispatch


class VanishedGatewayContinuationSendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name).resolve()
        self.preparation = _join_preparation()
        self.dispatch = _Dispatch()
        self.originals = (
            continuation_module.repo_scope_workspace_id,
            continuation_module.resolve_gateway_provider,
            continuation_module.mozyo_bridge_home,
        )
        continuation_module.repo_scope_workspace_id = lambda root: JOIN_WORKSPACE
        continuation_module.resolve_gateway_provider = lambda root: JOIN_PROVIDER
        continuation_module.mozyo_bridge_home = lambda: self.home

    def tearDown(self) -> None:
        (
            continuation_module.repo_scope_workspace_id,
            continuation_module.resolve_gateway_provider,
            continuation_module.mozyo_bridge_home,
        ) = self.originals
        self._temp.cleanup()

    def _record(self, **changes) -> IdentityAttestationRecord:
        values = dict(
            assigned_name=self.preparation.participant.assigned_name,
            workspace_id=JOIN_WORKSPACE,
            role=JOIN_PROVIDER,
            lane_id=JOIN_LANE,
            locator=JOIN_FRESH,
            verdict=VERDICT_PRESENT,
            observed_at=OBSERVED,
            replacement_action_id=self.preparation.action_id,
        )
        values.update(changes)
        return IdentityAttestationRecord(**values)

    def _seed(self, **changes) -> IdentityAttestationRecord:
        return HerdrIdentityAttestationStore(home=self.home).upsert(
            self._record(**changes)
        )

    def _row(self, **changes) -> dict:
        values = {"foreground_cwd": str(ROOT)}
        values.update(changes)
        return _live_row(**values)

    def _ops(self, *, rows=None, dispatch=None, before_read=None, **changes) -> _Ops:
        return _Ops(
            repo_root=changes.pop("repo_root", ROOT),
            upstream_coordinator=changes.pop("upstream_coordinator", UPSTREAM),
            rows=rows if rows is not None else [[self._row()], [self._row()]],
            dispatch=dispatch if dispatch is not None else self.dispatch,
            before_read=before_read,
            **changes,
        )

    def test_exact_v2_authority_is_rechecked_then_canonical_send_is_attempted_once(self):
        record = self._seed()
        ops = self._ops()
        result = ops.send_once(self.preparation)

        self.assertEqual(result.status, DRAIN_SEND_OK)
        self.assertEqual(result.detail, SEND_ATTEMPTED)
        self.assertTrue(result.attempted)
        self.assertFalse(result.zero_send)
        self.assertEqual(ops.reads, 2)
        self.assertEqual(ops.builds, 1)
        self.assertEqual(len(self.dispatch.calls), 1)
        self.assertEqual(
            self.dispatch.calls[0],
            {
                "issue": self.preparation.pointer.issue_id,
                "journal": self.preparation.pointer.journal_id,
                "gateway_pane": JOIN_FRESH,
                "lane_label": JOIN_LANE,
                "upstream_coordinator": UPSTREAM,
                "target_repo": str(ROOT),
            },
        )
        self.assertEqual(
            (
                result.action_id,
                result.workspace_id,
                result.lane_id,
                result.provider,
                result.assigned_name,
                result.fresh_locator,
                result.old_locator,
                result.observed_at,
            ),
            (
                self.preparation.action_id,
                JOIN_WORKSPACE,
                JOIN_LANE,
                JOIN_PROVIDER,
                self.preparation.participant.assigned_name,
                JOIN_FRESH,
                JOIN_OLD,
                record.observed_at,
            ),
        )
        for claim in ("complete", "confirm", "landed", "ledger"):
            self.assertNotIn(claim, f"{result.status} {result.detail}")

    def test_current_authority_is_one_read_only_fresh_snapshot(self):
        self._seed()
        ops = self._ops()
        authority = ops.current_authority(self.preparation)
        self.assertIsNotNone(authority)
        self.assertEqual(authority.fresh_locator, JOIN_FRESH)
        self.assertEqual(authority.observed_at, OBSERVED)
        self.assertEqual(ops.reads, 1)
        self.assertEqual(ops.builds, 0)
        self.assertEqual(self.dispatch.calls, [])

    def test_send_context_requires_exact_root_and_upstream(self):
        self.assertTrue(self._ops().context_is_exact())
        self.assertFalse(self._ops(repo_root=Path(".")).context_is_exact())
        self.assertFalse(self._ops(upstream_coordinator=" up ").context_is_exact())

    def test_actual_recognized_v1_side_binding_can_attempt_the_same_send(self):
        path = herdr_identity_attestation_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA user_version=1")
            conn.execute(
                "CREATE TABLE herdr_identity_attestations ("
                "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                "role TEXT NOT NULL, lane_id TEXT NOT NULL, locator TEXT NOT NULL, "
                "verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', "
                "observed_at TEXT NOT NULL)"
            )
        record = HerdrIdentityAttestationStore(home=self.home).upsert(
            self._record(replacement_action_id="")
        )
        side = HerdrIdentityReplacementBindingStore(home=self.home)
        intent = side.reserve(
            action_id=self.preparation.action_id,
            assigned_name=record.assigned_name,
            workspace_id=record.workspace_id,
            role=record.role,
            lane_id=record.lane_id,
            old_locator=JOIN_OLD,
            startup_nonce="nonce-14741",
            startup_action_id="startup-14741",
        )
        side.bind(
            intent,
            attestation=record,
            receipt_startup_action_id="startup-14741",
            receipt_role=record.role,
            receipt_assigned_name=record.assigned_name,
            receipt_locator=record.locator,
            receipt_present=True,
        )

        result = self._ops().send_once(self.preparation)
        self.assertEqual(result.status, DRAIN_SEND_OK)
        self.assertEqual(len(self.dispatch.calls), 1)

    def test_production_composition_builds_the_canonical_herdr_ops(self):
        self._seed()
        constructed = []
        dispatched = []

        class _CanonicalOps:
            def __init__(self, **kwargs):
                constructed.append(kwargs)

            def dispatch_implementation_request(self, **kwargs):
                dispatched.append(kwargs)
                return 0

        original = send_module.HerdrSublaneActuatorOps
        send_module.HerdrSublaneActuatorOps = _CanonicalOps
        try:
            ops = VanishedGatewayContinuationOps(
                repo_root=ROOT,
                upstream_coordinator=UPSTREAM,
                env={"SAFE": "1"},
                quiet_stdout=True,
                timeout=4.5,
            )
            reads = []

            def rows():
                reads.append(1)
                return [self._row()]

            ops._rows = rows
            result = ops.send_once(self.preparation)
        finally:
            send_module.HerdrSublaneActuatorOps = original
        self.assertEqual(result.status, DRAIN_SEND_OK)
        self.assertEqual(reads, [1, 1])
        self.assertEqual(
            constructed,
            [
                {
                    "repo_root": ROOT,
                    "lane_label": JOIN_LANE,
                    "issue": self.preparation.pointer.issue_id,
                    "journal": self.preparation.pointer.journal_id,
                    "env": {"SAFE": "1"},
                    "runner": None,
                    "quiet_stdout": True,
                    "timeout": 4.5,
                }
            ],
        )
        self.assertEqual(len(dispatched), 1)

    def test_locator_or_revision_move_between_checks_is_a_proven_zero_send(self):
        self._seed()
        cases = (
            (
                "locator",
                [
                    [self._row()],
                    [self._row(pane_id=FRESH_TWO)],
                ],
            ),
            (
                "revision",
                [
                    [self._row(revision=1)],
                    [self._row(revision=2)],
                ],
            ),
        )
        for label, rows in cases:
            with self.subTest(label=label):
                dispatch = _Dispatch()
                result = self._ops(rows=rows, dispatch=dispatch).send_once(
                    self.preparation
                )
                self.assertEqual(result.status, DRAIN_SEND_ZERO)
                self.assertEqual(result.detail, SEND_AUTHORITY_MOVED)
                self.assertEqual(dispatch.calls, [])

    def test_action_binding_move_between_checks_is_a_proven_zero_send(self):
        self._seed()

        def mutate(second_read):
            if second_read == 2:
                self._seed(replacement_action_id="foreign-action")

        result = self._ops(before_read=mutate).send_once(self.preparation)
        self.assertEqual(result.status, DRAIN_SEND_ZERO)
        self.assertEqual(result.detail, SEND_AUTHORITY_MOVED)
        self.assertEqual(self.dispatch.calls, [])

    def test_attestation_timestamp_move_between_checks_is_a_proven_zero_send(self):
        self._seed()

        def mutate(second_read):
            if second_read == 2:
                self._seed(observed_at="2026-08-03T01:02:04+00:00")

        result = self._ops(before_read=mutate).send_once(self.preparation)
        self.assertEqual(result.status, DRAIN_SEND_ZERO)
        self.assertEqual(result.detail, SEND_AUTHORITY_MOVED)
        self.assertEqual(self.dispatch.calls, [])

    def test_invalid_target_or_caller_authority_never_dispatches(self):
        self._seed()
        foreign_root = self.home / "foreign"
        foreign_root.mkdir()
        cases = (
            ("relative root", self._ops(repo_root=Path(".")), self.preparation),
            (
                "foreign row cwd",
                self._ops(rows=[[self._row(foreground_cwd=str(foreign_root))]]),
                self.preparation,
            ),
            ("empty upstream", self._ops(upstream_coordinator=""), self.preparation),
            ("padded upstream", self._ops(upstream_coordinator=" up "), self.preparation),
            (
                "foreign pointer shape",
                self._ops(),
                dataclasses.replace(self.preparation, pointer=object()),
            ),
            (
                "foreign preparation shape",
                self._ops(),
                object(),
            ),
        )
        for label, ops, preparation in cases:
            with self.subTest(label=label):
                result = ops.send_once(preparation)
                self.assertEqual(result.status, DRAIN_SEND_ZERO)
                self.assertEqual(result.detail, SEND_AUTHORITY_INVALID)
        self.assertEqual(self.dispatch.calls, [])

    def test_absent_duplicate_stale_foreign_or_unbound_authority_never_dispatches(self):
        cases = (
            ("absent", [], {}),
            ("duplicate", [self._row(), self._row()], {}),
            ("old locator", [self._row(pane_id=JOIN_OLD)], {}),
            ("stale shell", [self._row(agent="", status="unknown")], {}),
            ("foreign provider", [self._row(agent="claude")], {}),
            ("wrong action", [self._row()], {"replacement_action_id": "foreign"}),
            ("wrong locator", [self._row()], {"locator": FRESH_TWO}),
        )
        for label, rows, record_changes in cases:
            with self.subTest(label=label):
                dispatch = _Dispatch()
                self._seed(**record_changes)
                result = self._ops(
                    rows=[rows], dispatch=dispatch
                ).send_once(self.preparation)
                self.assertEqual(result.status, DRAIN_SEND_ZERO)
                self.assertEqual(result.detail, SEND_AUTHORITY_INVALID)
                self.assertEqual(dispatch.calls, [])

    def test_nonzero_unknown_or_exception_after_call_is_error_not_zero_send(self):
        self._seed()
        cases = (
            ("nonzero", _Dispatch(rc=7)),
            ("bool", _Dispatch(rc=False)),
            ("unknown", _Dispatch(rc=None)),
            ("exception", _Dispatch(error=RuntimeError("private path"))),
            ("system exit", _Dispatch(error=SystemExit(2))),
        )
        for label, dispatch in cases:
            with self.subTest(label=label):
                result = self._ops(dispatch=dispatch).send_once(self.preparation)
                self.assertEqual(result.status, DRAIN_SEND_ERROR)
                self.assertEqual(result.detail, SEND_FAILED)
                self.assertFalse(result.zero_send)
                self.assertEqual(len(dispatch.calls), 1)
                self.assertNotIn("private path", result.detail)

    def test_public_constructor_exposes_no_locator_provider_anchor_or_attestation_home(self):
        import inspect

        parameters = set(inspect.signature(VanishedGatewayContinuationOps).parameters)
        for forbidden in (
            "locator",
            "provider",
            "assigned_name",
            "pointer",
            "attestation_home",
            "list_rows",
            "dispatch",
        ):
            self.assertNotIn(forbidden, parameters)

    def test_send_edge_opens_no_delivery_ledger_or_replacement_transaction_store(self):
        import mozyo_bridge.core.state.herdr_delivery_ledger as ledger
        import mozyo_bridge.core.state.replacement_transaction as transaction

        self._seed()
        opened = []

        def forbidden(*args, **kwargs):
            opened.append(1)
            raise AssertionError("forbidden completion authority")

        originals = (ledger.HerdrDeliveryLedger, transaction.ReplacementTransactionStore)
        ledger.HerdrDeliveryLedger = forbidden
        transaction.ReplacementTransactionStore = forbidden
        try:
            result = self._ops().send_once(self.preparation)
        finally:
            ledger.HerdrDeliveryLedger, transaction.ReplacementTransactionStore = originals
        self.assertEqual(result.status, DRAIN_SEND_OK)
        self.assertEqual(opened, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
