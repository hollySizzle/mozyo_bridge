"""Regression coverage for `sublane rebind-restored-pair` (Redmine #15656).

A herdr server restart restores an ACTIVE lane's gateway+worker pair (the same
agent sessions) onto new pane locators while the lifecycle row keeps pinning
the old locators. The rail CAS-replaces ONLY the ``declared_slots`` snapshot
from live attested evidence; every gate failure is zero-write with a typed
reason and ``lane_generation`` never changes.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_SUPERSEDED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_lifecycle_model import decode_declared_slots
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind import (  # noqa: E501
    SublaneRestoredPairRebindUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_cli import (  # noqa: E501
    register_sublane_rebind_restored_pair_parser,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_restored_pair_rebind_live import (  # noqa: E501
    LiveRestoredPairRebindOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.restored_pair_rebind import (  # noqa: E501
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_PREFLIGHT,
    STATUS_REFUSED,
    RestoredPairRebindRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

ISSUE = "15656"
JOURNAL = "107711"
WS = "ws_main"
LANE = "issue_15656_lane"
TOKEN = "wt_issue_15656_token"
GW_PROVIDER = "codex"
WK_PROVIDER = "claude"
GW_NAME = encode_assigned_name(WS, GW_PROVIDER, LANE)
WK_NAME = encode_assigned_name(WS, WK_PROVIDER, LANE)
GW_OLD = "w1:%1"
WK_OLD = "w1:%2"
GW_NEW = "w9:%11"
WK_NEW = "w9:%12"
KEY = LaneLifecycleKey(WS, LANE)
DECISION = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)


def _pin(role: str, provider: str, name: str, locator: str) -> ProcessGenerationPin:
    return ProcessGenerationPin(
        role=role, provider=provider, assigned_name=name, locator=locator
    )


def _old_pair() -> tuple[ProcessGenerationPin, ProcessGenerationPin]:
    return (
        _pin("gateway", GW_PROVIDER, GW_NAME, GW_OLD),
        _pin("worker", WK_PROVIDER, WK_NAME, WK_OLD),
    )


def _row(
    name: str,
    locator: str,
    terminal: str,
    provider: str,
    *,
    runtime_revision: str = "",
) -> dict:
    row = {
        "name": name,
        "pane_id": locator,
        "terminal_id": terminal,
        "provider": provider,
        "agent": provider,
    }
    if runtime_revision:
        row["runtime_revision"] = runtime_revision
    return row


def _restored_rows() -> list[dict]:
    return [
        _row(GW_NAME, GW_NEW, "term-gw-2", GW_PROVIDER),
        _row(WK_NAME, WK_NEW, "term-wk-2", WK_PROVIDER, runtime_revision="cli-2.1.0"),
    ]


class _TestOps(LiveRestoredPairRebindOps):
    """Live ops with the host-probe seams faked; the store joins stay real."""

    def __init__(self, home: Path, rows, *, providers=(GW_PROVIDER, WK_PROVIDER)):
        super().__init__(
            repo_root=Path("/lane/issue_15656"),
            env={},
            lifecycle_home=home,
            attestation_home=home,
        )
        self.test_rows = list(rows)
        self.test_providers = providers

    def _resolve_root(self):
        return self.repo_root

    def _workspace_id(self, root):
        return WS

    def _worktree_identity(self, root, lane):
        return TOKEN

    def _worktree_readable(self, root):
        return True

    def _branch(self, root):
        return LANE

    def _providers(self, root):
        return self.test_providers

    def _rows(self):
        return list(self.test_rows)


class _StaleReadOps(_TestOps):
    """Caches the FIRST lifecycle read, simulating a revision race at the CAS."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cached = None

    def _lifecycle_record(self, workspace_id, lane):
        if self._cached is None:
            self._cached = super()._lifecycle_record(workspace_id, lane)
        return self._cached


class _Base(unittest.TestCase):
    def _temp_home(self, prefix: str = "mzb-15656-") -> Path:
        tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def setUp(self):
        self.home = self._temp_home()

    def declare(self, *, slots=None):
        outcome = LaneDeclarationStore(home=self.home).declare_lane(
            KEY,
            decision=DECISION,
            binding_kind=BINDING_KIND_ISSUE,
            issue_id=ISSUE,
            declared_slots=_old_pair() if slots is None else slots,
            worktree_identity=TOKEN,
        )
        self.assertTrue(outcome.applied, outcome.reason)

    def attest(self, name: str, locator: str, terminal: str, provider: str):
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=name,
                workspace_id=WS,
                role=provider,
                lane_id=LANE,
                locator=locator,
                verdict=VERDICT_PRESENT,
                terminal_id=terminal,
            )
        )

    def attest_restored_pair(self):
        self.attest(GW_NAME, GW_NEW, "term-gw-2", GW_PROVIDER)
        self.attest(WK_NAME, WK_NEW, "term-wk-2", WK_PROVIDER)

    def record(self):
        return LaneLifecycleStore(home=self.home).get(KEY)

    def run_rail(self, ops, *, execute: bool):
        return SublaneRestoredPairRebindUseCase(ops).run(
            RestoredPairRebindRequest(issue=ISSUE, lane=LANE, journal=JOURNAL),
            execute=execute,
        )

    def assert_zero_write(self, *, revision: int = 1):
        record = self.record()
        self.assertIsNotNone(record)
        self.assertEqual(record.revision, revision)
        pins = decode_declared_slots(record.declared_slots)
        self.assertEqual(
            sorted(pin.locator for pin in pins), sorted([GW_OLD, WK_OLD])
        )


class RestoredPairRebindSuccessTests(_Base):
    def test_preflight_reports_ready_and_writes_nothing(self):
        self.declare()
        self.attest_restored_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _restored_rows()), execute=False
        )
        self.assertEqual(outcome.status, STATUS_PREFLIGHT)
        self.assertFalse(outcome.executed)
        self.assertTrue(outcome.plan.may_rebind, outcome.plan.blocked_reasons)
        self.assertEqual(outcome.detail, "rebind_ready")
        self.assert_zero_write()

    def test_execute_replaces_declared_slots_with_live_evidence(self):
        self.declare()
        self.attest_restored_pair()
        outcome = self.run_rail(_TestOps(self.home, _restored_rows()), execute=True)
        self.assertEqual(outcome.status, STATUS_COMPLETED)
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.revision, 2)

        record = self.record()
        self.assertEqual(record.revision, 2)
        # The restored processes are the SAME agent-session incarnation: the
        # generation counter must not move (dispatch-marker anchors stay valid).
        self.assertEqual(record.lane_generation, 1)
        self.assertEqual(record.lane_disposition, DISPOSITION_ACTIVE)
        pair = read_declared_pin_pair(record)
        self.assertTrue(pair.ok, pair.reason)
        self.assertEqual(pair.gateway.locator, GW_NEW)
        self.assertEqual(pair.worker.locator, WK_NEW)
        self.assertEqual(pair.gateway.assigned_name, GW_NAME)
        self.assertEqual(pair.worker.assigned_name, WK_NAME)
        # The live row surfaced a runtime revision; the rebound pin carries it.
        self.assertEqual(pair.worker.runtime_revision, "cli-2.1.0")

    def test_rebound_pin_passes_worker_dispatch_generation_binding(self):
        self.declare()
        self.attest_restored_pair()
        self.run_rail(_TestOps(self.home, _restored_rows()), execute=True)
        pair = read_declared_pin_pair(self.record())
        live_worker = ProcessGenerationPin(
            role=pair.worker.role,
            provider=WK_PROVIDER,
            assigned_name=WK_NAME,
            locator=WK_NEW,
            runtime_revision="cli-2.1.0",
        )
        # The exact admission join `sublane dispatch-worker` performs (#13846):
        # the rebound declared pin now binds the live generation...
        self.assertTrue(pair.worker.binds_same_generation(live_worker))
        # ...while the pre-rebind stale pin was the false
        # `worker_liveness_authority_conflict` this rail repairs.
        old_worker = _old_pair()[1]
        self.assertFalse(old_worker.binds_same_generation(live_worker))


class RestoredPairRebindFailClosedTests(_Base):
    def _blocked(self, ops, reason_fragment: str, *, revision: int = 1):
        outcome = self.run_rail(ops, execute=True)
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertFalse(outcome.applied)
        self.assertIn(reason_fragment, ",".join(outcome.plan.blocked_reasons))
        self.assert_zero_write(revision=revision)
        return outcome

    def test_duplicate_assigned_name_is_zero_write(self):
        self.declare()
        self.attest_restored_pair()
        rows = _restored_rows() + [
            _row(GW_NAME, "w9:%99", "term-gw-dup", GW_PROVIDER)
        ]
        self._blocked(
            _TestOps(self.home, rows), "duplicate_live_candidates:gateway"
        )

    def test_missing_attestation_is_zero_write(self):
        self.declare()
        # Only the gateway attests; the worker record is absent.
        self.attest(GW_NAME, GW_NEW, "term-gw-2", GW_PROVIDER)
        self._blocked(
            _TestOps(self.home, _restored_rows()), "unattested_slot:worker"
        )

    def test_attestation_bound_to_old_locator_is_zero_write(self):
        self.declare()
        self.attest(GW_NAME, GW_NEW, "term-gw-2", GW_PROVIDER)
        # A stale record from the pre-restart generation is never re-used.
        self.attest(WK_NAME, WK_OLD, "term-wk-old", WK_PROVIDER)
        self._blocked(
            _TestOps(self.home, _restored_rows()), "unattested_slot:worker"
        )

    def test_live_equals_declared_is_zero_write(self):
        self.declare()
        rows = [
            _row(GW_NAME, GW_OLD, "term-gw-1", GW_PROVIDER),
            _row(WK_NAME, WK_OLD, "term-wk-1", WK_PROVIDER),
        ]
        self.attest(GW_NAME, GW_OLD, "term-gw-1", GW_PROVIDER)
        self.attest(WK_NAME, WK_OLD, "term-wk-1", WK_PROVIDER)
        outcome = self._blocked(
            _TestOps(self.home, rows), "locator_not_drifted:gateway"
        )
        self.assertIn(
            "locator_not_drifted:worker", ",".join(outcome.plan.blocked_reasons)
        )

    def test_single_passing_slot_never_partially_updates(self):
        self.declare()
        # The worker moved and attests on its new locator; the gateway did not
        # move. All-or-nothing: nothing may be written.
        rows = [
            _row(GW_NAME, GW_OLD, "term-gw-1", GW_PROVIDER),
            _row(WK_NAME, WK_NEW, "term-wk-2", WK_PROVIDER),
        ]
        self.attest(GW_NAME, GW_OLD, "term-gw-1", GW_PROVIDER)
        self.attest(WK_NAME, WK_NEW, "term-wk-2", WK_PROVIDER)
        outcome = self._blocked(
            _TestOps(self.home, rows), "locator_not_drifted:gateway"
        )
        self.assertTrue(outcome.plan.worker.ready)

    def test_non_active_dispositions_are_zero_write(self):
        for target in (DISPOSITION_HIBERNATED, DISPOSITION_SUPERSEDED):
            with self.subTest(target=target):
                home = self._temp_home(prefix="mzb-15656-disp-")
                self.home = home
                self.declare()
                self.attest_restored_pair()
                moved = LaneLifecycleStore(home=home).transition_disposition(
                    KEY,
                    expected_disposition=DISPOSITION_ACTIVE,
                    expected_revision=1,
                    target=target,
                    decision=DECISION,
                )
                self.assertTrue(moved.applied, moved.reason)
                outcome = self.run_rail(
                    _TestOps(home, _restored_rows()), execute=True
                )
                self.assertEqual(outcome.status, STATUS_BLOCKED)
                self.assertIn("lane_not_active", outcome.plan.blocked_reasons)
                record = self.record()
                self.assertEqual(record.revision, 2)
                pins = decode_declared_slots(record.declared_slots)
                self.assertEqual(
                    sorted(pin.locator for pin in pins),
                    sorted([GW_OLD, WK_OLD]),
                )

    def test_revision_race_is_zero_write_with_typed_reason(self):
        self.declare()
        self.attest_restored_pair()
        ops = _StaleReadOps(self.home, _restored_rows())
        # Prime the stale cache at revision 1...
        self.assertTrue(ops.observe(
            RestoredPairRebindRequest(issue=ISSUE, lane=LANE)
        ).may_rebind)
        # ...then a concurrent writer moves the row (hibernate + rehydrate).
        store = LaneLifecycleStore(home=self.home)
        self.assertTrue(
            store.transition_disposition(
                KEY,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=DECISION,
            ).applied
        )
        self.assertTrue(
            store.transition_disposition(
                KEY,
                expected_disposition=DISPOSITION_HIBERNATED,
                expected_revision=2,
                target=DISPOSITION_ACTIVE,
                decision=DECISION,
            ).applied
        )
        outcome = self.run_rail(ops, execute=True)
        self.assertEqual(outcome.status, STATUS_REFUSED)
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.detail, "stale_revision")
        self.assert_zero_write(revision=3)

    def test_empty_declared_slots_is_zero_write(self):
        self.declare(slots=())
        self.attest_restored_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _restored_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        self.assertIn("declared_slots_unresolved", outcome.plan.blocked_reasons)
        record = self.record()
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.declared_slots, "")

    def test_provider_mismatch_is_zero_write(self):
        # The declared pins bind the slots to a SWAPPED provider pair.
        self.declare(
            slots=(
                _pin("gateway", WK_PROVIDER, GW_NAME, GW_OLD),
                _pin("worker", GW_PROVIDER, WK_NAME, WK_OLD),
            )
        )
        self.attest_restored_pair()
        outcome = self.run_rail(
            _TestOps(self.home, _restored_rows()), execute=True
        )
        self.assertEqual(outcome.status, STATUS_BLOCKED)
        joined = ",".join(outcome.plan.blocked_reasons)
        self.assertIn("provider_mismatch:gateway", joined)
        self.assertIn("provider_mismatch:worker", joined)
        record = self.record()
        self.assertEqual(record.revision, 1)
        pins = decode_declared_slots(record.declared_slots)
        self.assertEqual(
            sorted(pin.locator for pin in pins), sorted([GW_OLD, WK_OLD])
        )

    def test_declared_locator_still_live_is_zero_write(self):
        self.declare()
        self.attest_restored_pair()
        # The old gateway locator is still a live (unmanaged) slot: this is not
        # the restore-moved-the-pair shape.
        rows = _restored_rows() + [
            {"pane_id": GW_OLD, "terminal_id": "term-res-1", "name": None}
        ]
        self._blocked(
            _TestOps(self.home, rows), "declared_locator_still_live:gateway"
        )


class RestoredPairRebindCliContractTests(unittest.TestCase):
    def test_parser_registers_read_only_default(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="sublane_command")
        register_sublane_rebind_restored_pair_parser(sub)
        args = parser.parse_args(
            ["rebind-restored-pair", "--issue", ISSUE, "--lane", LANE]
        )
        self.assertFalse(args.execute)
        self.assertEqual(args.journal, "")
        self.assertFalse(args.json)
        self.assertTrue(callable(args.func))


if __name__ == "__main__":
    unittest.main()
