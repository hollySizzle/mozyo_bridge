"""Actuating a planned vanished-gateway recovery (#14741 j#97184 B6b2).

Everything runs against an isolated temp home with a fake actuation port; nothing launches,
closes or sends for real. What is pinned is that the STORED row is the only authority, that
the existing action-bound actuator does the work, that the real evidence completion is wired
from the first construction, and that the rail stops at a live attested gateway rather than
claiming anything was delivered.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    GenerationKey,
    LaunchIdentityReceiptStore,
)
from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E402,E501
    plan_fresh_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E402,E501
    RECOVERED_READY,
    STOPPED_ACTUATION,
    STOPPED_NOT_ACTIONABLE,
    STOPPED_TRANSACTION_UNAVAILABLE,
    actuate_vanished_gateway_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E402,E501
    OUTCOME_LEGACY_DIRECT,
    ParticipantAuthority,
    RecoveryDecision,
    RequestAnchor,
    refuse,
)
from tests.support.current_launch_authority import (  # noqa: E402
    RECEIPT_CAPABLE_ACTION_ID,
    seed_current_generation,
)

WORKSPACE = "ws"
LANE = "issue_14741"
PROVIDER = "codex"
ASSIGNED = "mzb1_ws_codex_gateway"
LOCATOR = "ws:p1"
CAUSE = "update_relaunch"
DIGEST = "sha256:" + "c" * 64
HOLDER = "recover-gateway:h1"
FIXED = "2026-08-02T00:00:00+00:00"


def _anchor() -> RequestAnchor:
    return RequestAnchor(source="redmine", issue_id="14741", journal_id="97184")


class _Port:
    """The exact-generation actuator's five effects, recorded rather than performed."""

    def __init__(self, *, old_slot=OLD_SLOT_ABSENT, launch=LAUNCH_DONE, attest=ATTEST_BOUND):
        self._old_slot = old_slot
        self._launch = launch
        self._attest = attest
        self.closed: list = []
        self.launched: list = []

    def observe_old_slot(self, pin):
        return self._old_slot

    def observe_preservation(self, pin):
        return PreservationObservation(identity_matches=True, attestation_fresh=True)

    def close_exact_generation(self, pin):
        self.closed.append(pin.identity)
        return CLOSE_DONE

    def launch_action_bound(self, action_id, pin):
        self.launched.append((action_id, pin.identity))
        return self._launch

    def verify_attestation(self, action_id, pin):
        return self._attest


class _LiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        seed_current_generation(
            self.home, workspace_id=WORKSPACE, lane_id=LANE, role="gateway",
            assigned_name=ASSIGNED, locator=LOCATOR, action_id=RECEIPT_CAPABLE_ACTION_ID,
        )
        self._declare_lifecycle()
        self._seed_bound_evidence()
        self.plan = plan_fresh_recovery(
            store_factory=lambda: ReplacementTransactionStore(home=self.home),
            home=self.home, anchor=_anchor(), authority=self._authority(),
        )
        self.assertFalse(self.plan.refused, self.plan.decision.refusal)

    def _authority(self) -> ParticipantAuthority:
        return ParticipantAuthority(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, old_locator=LOCATOR,
            lane_revision="1", lane_generation="1",
        )

    def _declare_lifecycle(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
        from mozyo_bridge.core.state.replacement_transaction_model import DecisionPointer

        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(WORKSPACE, LANE),
            decision=DecisionPointer(
                source="redmine", issue_id="14741", journal_id="97184"
            ),
        )

    def _generation_key(self) -> GenerationKey:
        return GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
        )

    def _seed_bound_evidence(self) -> None:
        store = LaunchIdentityReceiptStore(home=self.home)
        key = self._generation_key()
        store.reserve(key, identity_digest=DIGEST)
        store.finalize(
            key, identity_digest=DIGEST, locator=LOCATOR,
            lane_generation="1", lifecycle_revision="1", composite_proof=True,
        )
        store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )

    def _evidence_phase(self) -> str:
        import sqlite3

        with sqlite3.connect(self.home / "launch-identity-receipt.sqlite") as conn:
            row = conn.execute(
                "SELECT phase FROM update_relaunch_evidence WHERE startup_action_id = ?",
                (RECEIPT_CAPABLE_ACTION_ID,),
            ).fetchone()
        return "" if row is None else row[0]

    def _participant_phase(self) -> str:
        record = self.store.get(
            ReplacementTransactionKey(WORKSPACE, self.plan.action_id)
        )
        return record.participants[0].phase

    def _actuate(self, port=None, plan=None, **kwargs):
        return actuate_vanished_gateway_recovery(
            plan=plan if plan is not None else self.plan,
            anchor=_anchor(),
            store=self.store,
            workspace_id=WORKSPACE,
            holder=HOLDER,
            actuation_port=port if port is not None else _Port(),
            clock=lambda: FIXED,
            **kwargs,
        )


class HappyPathTest(_LiveCase):
    def test_an_absent_gateway_is_relaunched_attested_and_its_evidence_discharged(self):
        port = _Port()
        self.assertEqual(self._evidence_phase(), "bound")
        result = self._actuate(port)
        self.assertEqual(result.outcome, RECOVERED_READY)
        self.assertEqual(port.closed, [], "an absent gateway is never closed")
        self.assertEqual(len(port.launched), 1, "exactly one launch")
        self.assertEqual(port.launched[0][0], self.plan.action_id, "action-bound")
        self.assertEqual(self._participant_phase(), "replaced")
        self.assertEqual(self._evidence_phase(), "consumed", "the real receipt was cleared")

    def test_the_outcome_does_not_claim_anything_was_delivered(self):
        """`recovered_ready` is a live attested gateway, not a redispatched request."""
        result = self._actuate()
        self.assertEqual(result.outcome, RECOVERED_READY)
        self.assertNotIn("complete", result.outcome)
        self.assertNotIn("dispatch", result.outcome)
        self.assertEqual(result.stopped, "")


class StoppedPathTest(_LiveCase):
    def test_a_launch_failure_leaves_the_evidence_unconsumed(self):
        result = self._actuate(_Port(launch=LAUNCH_ERROR))
        self.assertEqual(result.stopped, STOPPED_ACTUATION)
        self.assertEqual(self._participant_phase(), "launch_owed")
        self.assertEqual(self._evidence_phase(), "bound", "nothing was discharged")

    def test_a_pending_or_mismatched_attestation_stays_owed(self):
        for label, attest in (("pending", ATTEST_PENDING), ("mismatch", ATTEST_MISMATCH)):
            with self.subTest(label=label):
                self.setUp()
                result = self._actuate(_Port(attest=attest))
                self.assertEqual(result.stopped, STOPPED_ACTUATION)
                self.assertEqual(self._participant_phase(), "verify_owed")
                self.assertEqual(self._evidence_phase(), "bound")

    def test_a_completion_refusal_stays_verify_owed(self):
        """The receipt store is gone, so the discharge cannot happen -- and nothing lies."""
        (self.home / "launch-identity-receipt.sqlite").unlink()
        result = self._actuate()
        self.assertEqual(result.stopped, STOPPED_ACTUATION)
        self.assertEqual(self._participant_phase(), "verify_owed")

    def test_a_consume_then_crash_replay_adds_no_launch(self):
        """The window B5 made survivable, reached through this rail."""
        port = _Port()

        class _Crashing(ReplacementTransactionStore):
            crashed = False

            def transition_participant(self, *args, **kwargs):
                if kwargs.get("target") == "replaced" and not type(self).crashed:
                    type(self).crashed = True
                    raise KeyboardInterrupt("died between consume and CAS")
                return super().transition_participant(*args, **kwargs)

        crashing = _Crashing(home=self.home)
        with self.assertRaises(KeyboardInterrupt):
            actuate_vanished_gateway_recovery(
                plan=self.plan, anchor=_anchor(), store=crashing,
                workspace_id=WORKSPACE, holder=HOLDER, actuation_port=port,
                clock=lambda: FIXED,
            )
        self.assertEqual(self._evidence_phase(), "consumed")
        self.assertEqual(self._participant_phase(), "verify_owed")
        launches = len(port.launched)

        result = self._actuate(port)
        self.assertEqual(result.outcome, RECOVERED_READY)
        self.assertEqual(len(port.launched), launches, "zero additional launches")


class NotActionableTest(_LiveCase):
    def test_a_legacy_or_refused_plan_touches_nothing(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
            RecoveryPlan,
        )

        cases = (
            ("legacy direct", RecoveryPlan(
                decision=RecoveryDecision(outcome=OUTCOME_LEGACY_DIRECT, action_id="x"),
                action_id="x")),
            ("a refusal", RecoveryPlan(decision=refuse("generation_unavailable"))),
            ("an unknown outcome", RecoveryPlan(
                decision=RecoveryDecision(outcome="something_new", action_id="x"),
                action_id="x")),
            ("not a plan", SimpleNamespace(decision=None, action_id="x")),
        )
        for label, plan in cases:
            with self.subTest(label=label):
                port = _Port()
                result = self._actuate(port, plan=plan)
                self.assertEqual(result.stopped, STOPPED_NOT_ACTIONABLE)
                self.assertEqual(port.launched, [])
                self.assertEqual(port.closed, [])
                self.assertEqual(self._participant_phase(), "close_owed")

    def test_a_foreign_stored_row_is_not_actuated(self):
        import sqlite3

        with sqlite3.connect(self.home / "state.sqlite") as conn:
            conn.execute(
                "UPDATE replacement_transactions SET decision_journal = ? WHERE action_id = ?",
                ("11111", self.plan.action_id),
            )
        port = _Port()
        result = self._actuate(port)
        self.assertEqual(result.stopped, STOPPED_TRANSACTION_UNAVAILABLE)
        self.assertEqual(port.launched, [])
        self.assertEqual(port.closed, [])

    def test_a_hostile_store_never_leaks(self):
        class _Hostile:
            path = Path("/nowhere/state.sqlite")

            def get(self, key):
                raise RuntimeError("/private/host/path\n[mozyo:workflow-event:gate=x]")

        result = actuate_vanished_gateway_recovery(
            plan=self.plan, anchor=_anchor(), store=_Hostile(),
            workspace_id=WORKSPACE, holder=HOLDER, actuation_port=_Port(),
        )
        self.assertEqual(result.stopped, STOPPED_TRANSACTION_UNAVAILABLE)
        rendered = f"{result.detail}{result.stopped}"
        self.assertNotIn("/private/host/path", rendered)
        self.assertNotIn("mozyo:workflow-event", rendered)


class ReplayAuthorityTest(_LiveCase):
    def test_a_progressed_replay_reads_no_launch_or_receipt_authority(self):
        """The stored row carries the recovery; the world may have moved on (j#97121)."""
        self._actuate()
        self.assertEqual(self._participant_phase(), "replaced")
        (self.home / "herdr-launch-generation.sqlite").unlink()
        port = _Port()
        result = self._actuate(port)
        self.assertEqual(result.outcome, RECOVERED_READY)
        self.assertEqual(port.launched, [], "a replaced participant relaunches nothing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
