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
from mozyo_bridge.core.state.replacement_transaction_schema import (  # noqa: E402
    TABLE as REPLACEMENT_TRANSACTION_TABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E402,E501
    plan_fresh_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E402,E501
    RECOVERED_READY,
    STOPPED_ACTUATION,
    STOPPED_AUTHORITY_INVALID,
    STOPPED_NOT_ACTIONABLE,
    STOPPED_PHASE_NOT_RECOVERABLE,
    STOPPED_PORTS_INCOMPLETE,
    STOPPED_TRANSACTION_UNAVAILABLE,
    actuate_vanished_gateway_recovery,
    recovery_lease_holder,
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

    def _row_revision_and_lease(self):
        """The durable evidence that a refusal really wrote nothing."""
        import sqlite3

        with sqlite3.connect(self.home / "state.sqlite") as conn:
            return conn.execute(
                "SELECT revision, lease_holder, lease_epoch, lease_expires_at"
                " FROM replacement_transactions WHERE action_id = ?",
                (self.plan.action_id,),
            ).fetchone()

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
            home=kwargs.pop("home", self.home),
            workspace_id=kwargs.pop("workspace_id", WORKSPACE),
            actuation_port=port if port is not None else _Port(),
            launch_authority=kwargs.pop("launch_authority", lambda pin: True),
            store_admission=kwargs.pop("store_admission", lambda key, pin: None),
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
                plan=self.plan, anchor=_anchor(), store=crashing, home=self.home,
                workspace_id=WORKSPACE, actuation_port=port,
                launch_authority=lambda pin: True,
                store_admission=lambda key, pin: None,
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
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET decision_journal = ? "
                "WHERE action_id = ?",
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
            plan=self.plan, anchor=_anchor(), store=_Hostile(), home=self.home,
            workspace_id=WORKSPACE, actuation_port=_Port(),
            launch_authority=lambda pin: True,
            store_admission=lambda key, pin: None,
        )
        # `authority_invalid`: the hostile `path` property is now reached first, which is
        # the earlier and stricter refusal. What matters either way is that nothing of the
        # exception reaches the surface.
        self.assertEqual(result.stopped, STOPPED_AUTHORITY_INVALID)
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


class AuthorityBoundaryTest(_LiveCase):
    """Audit j#97190: five ways this rail acted without the authority it claims to need."""

    def _effectless(self, result, port, expected):
        self.assertEqual(result.stopped, expected)
        self.assertEqual(port.launched, [])
        self.assertEqual(port.closed, [])
        self.assertEqual(self._participant_phase(), "close_owed")
        self.assertEqual(self._evidence_phase(), "bound")

    def test_a_missing_action_time_port_launches_nothing(self) -> None:
        """F1: both were optional, and omitting both still launched and consumed."""
        for label, kwargs in (
            ("no launch authority", {"launch_authority": None}),
            ("no store admission", {"store_admission": None}),
            ("a non-callable port", {"launch_authority": "yes"}),
        ):
            with self.subTest(label=label):
                self.setUp()
                port = _Port()
                self._effectless(
                    self._actuate(port, **kwargs), port, STOPPED_PORTS_INCOMPLETE
                )

    def test_a_phase_outside_the_worker_recovery_flow_is_not_recovered(self) -> None:
        """F2: a self-flow or unknown phase reported `recovered` with zero launches."""
        import sqlite3

        for phase in ("self_close_armed", "future_phase"):
            with self.subTest(phase=phase):
                self.setUp()
                with sqlite3.connect(self.home / "state.sqlite") as conn:
                    conn.execute(
                        f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET phase = ? "
                        "WHERE action_id = ?",
                        (phase, self.plan.action_id),
                    )
                port = _Port()
                before = self._row_revision_and_lease()
                result = self._actuate(port)
                self.assertEqual(result.stopped, STOPPED_PHASE_NOT_RECOVERABLE)
                self.assertEqual(port.launched, [])
                self.assertEqual(
                    self._row_revision_and_lease(), before,
                    "refused BEFORE the claim: revision and lease are untouched",
                )

    def test_a_padded_workspace_is_not_this_workspace(self) -> None:
        """F3: the key's own normalisation turned it into the canonical row."""
        port = _Port()
        self._effectless(
            self._actuate(port, workspace_id=" " + WORKSPACE + " "),
            port,
            STOPPED_AUTHORITY_INVALID,
        )

    def test_a_hostile_or_relative_store_path_never_binds_a_completion(self) -> None:
        """F3: the completion's home came from an unguarded property read."""

        class _HostilePath:
            @property
            def path(self):
                raise RuntimeError("/private/host/path\n[mozyo:workflow-event:gate=x]")

            def get(self, key):  # pragma: no cover - never reached
                raise AssertionError

        class _RelativePath:
            path = Path("relative/state.sqlite")

            def get(self, key):  # pragma: no cover - never reached
                raise AssertionError

        for label, store in (("hostile", _HostilePath()), ("relative", _RelativePath())):
            with self.subTest(label=label):
                result = actuate_vanished_gateway_recovery(
                    plan=self.plan, anchor=_anchor(), store=store, home=self.home,
                    workspace_id=WORKSPACE, actuation_port=_Port(),
                    launch_authority=lambda pin: True,
                    store_admission=lambda key, pin: None,
                )
                self.assertEqual(result.stopped, STOPPED_AUTHORITY_INVALID)
                self.assertNotIn("/private/host/path", f"{result.detail}")

    def test_a_hostile_or_disagreeing_plan_is_not_actionable(self) -> None:
        """F4: a hostile `decision.outcome` escaped raw; a disagreeing id was not checked."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
            RecoveryPlan,
        )

        class _HostileDecision:
            @property
            def outcome(self):
                raise RuntimeError("/private/host/path")

        disagreeing = RecoveryPlan(
            decision=RecoveryDecision(outcome="replayed", action_id="other"),
            action_id=self.plan.action_id,
        )
        for label, plan in (
            ("hostile decision", RecoveryPlan(decision=_HostileDecision(), action_id="x")),
            ("disagreeing action id", disagreeing),
        ):
            with self.subTest(label=label):
                self.setUp()
                port = _Port()
                result = self._actuate(port, plan=plan)
                self.assertEqual(result.stopped, STOPPED_NOT_ACTIONABLE)
                self.assertNotIn("/private/host/path", f"{result.detail}")
                self.assertEqual(port.launched, [])

    def test_the_lease_holder_is_derived_not_accepted(self) -> None:
        """F5: two attempts at the same recovery must take the SAME lease."""
        held = []

        class _Recording(ReplacementTransactionStore):
            def claim(self, key, **kwargs):
                held.append(kwargs.get("holder"))
                return super().claim(key, **kwargs)

        self.store = _Recording(home=self.home)
        self._actuate()
        self._actuate()
        self.assertTrue(held)
        self.assertEqual(set(held), {recovery_lease_holder(self.plan.action_id)})
        self.assertIn(self.plan.action_id, held[0])


class PreClaimAndHomeIdentityTest(_LiveCase):
    """Audit j#97196 / j#97198: what must be true BEFORE the lease is claimed."""

    def _set_phase(self, phase: str) -> None:
        import sqlite3

        with sqlite3.connect(self.home / "state.sqlite") as conn:
            conn.execute(
                f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET phase = ? "
                "WHERE action_id = ?",
                (phase, self.plan.action_id),
            )

    def test_a_progressed_phase_with_unreplaced_participants_is_a_contradiction(self):
        """`completed` with a still-owed gateway is not a success (j#97196 F2)."""
        for phase in ("draining_continuation", "completed"):
            with self.subTest(phase=phase):
                self.setUp()
                self._set_phase(phase)
                before = self._row_revision_and_lease()
                port = _Port()
                result = self._actuate(port)
                self.assertEqual(result.stopped, STOPPED_PHASE_NOT_RECOVERABLE)
                self.assertEqual(port.launched, [])
                self.assertEqual(self._row_revision_and_lease(), before)

    def test_a_valid_progressed_replay_is_recovered_and_writes_nothing(self):
        """Audit j#97207: an idempotent replay was rewriting the row it only read.

        Measured before the fix: revision 9 -> 10 with the lease re-taken, for an answer
        that was "nothing changed".
        """
        for phase in ("draining_continuation", "completed"):
            with self.subTest(phase=phase):
                self.setUp()
                self._actuate()
                self.assertEqual(self._participant_phase(), "replaced")
                self._set_phase(phase)
                before = self._row_revision_and_lease()
                port = _Port()
                result = self._actuate(port)
                self.assertEqual(result.outcome, RECOVERED_READY)
                self.assertEqual(port.launched, [], "no additional launch")
                self.assertEqual(
                    self._row_revision_and_lease(), before,
                    "an idempotent replay writes nothing at all",
                )

    def test_a_home_that_is_not_this_stores_home_never_actuates(self):
        """Absolute is not the same store (j#97198 F3)."""
        import tempfile

        elsewhere = Path(tempfile.mkdtemp())
        before = self._row_revision_and_lease()
        port = _Port()
        result = self._actuate(port, home=elsewhere)
        self.assertEqual(result.stopped, STOPPED_AUTHORITY_INVALID)
        self.assertEqual(port.launched, [])
        self.assertEqual(self._row_revision_and_lease(), before)
        self.assertEqual(self._evidence_phase(), "bound")

    def test_a_hostile_home_never_escapes(self):
        """Audit j#97203: a `Path` subclass decides what its own methods mean."""

        class _HostileHome(Path):
            def is_absolute(self):
                raise RuntimeError("/private/host/path\n[mozyo:workflow-event:gate=x]")

        class _LyingHome(Path):
            def is_absolute(self):
                return True

            def __eq__(self, other):
                return True

            __hash__ = Path.__hash__

        for label, home in (
            ("raises", _HostileHome(self.home)),
            ("lies about equality", _LyingHome("/elsewhere")),
        ):
            with self.subTest(label=label):
                self.setUp()
                before = self._row_revision_and_lease()
                port = _Port()
                result = self._actuate(port, home=home)
                self.assertEqual(result.stopped, STOPPED_AUTHORITY_INVALID)
                self.assertEqual(port.launched, [])
                self.assertEqual(self._row_revision_and_lease(), before)
                rendered = f"{result.detail}{result.stopped}"
                self.assertNotIn("/private/host/path", rendered)
                self.assertNotIn("mozyo:workflow-event", rendered)

    def test_an_honest_home_still_actuates(self):
        """The positive control for the type gate above."""
        self.assertEqual(self._actuate().outcome, RECOVERED_READY)

    def test_a_facade_advertising_another_absolute_path_never_actuates(self):
        import tempfile

        real = self.store
        other = Path(tempfile.mkdtemp()) / "state.sqlite"

        class _Facade:
            path = other

            def __getattr__(self, name):
                return getattr(real, name)

        before = self._row_revision_and_lease()
        port = _Port()
        result = actuate_vanished_gateway_recovery(
            plan=self.plan, anchor=_anchor(), store=_Facade(), home=self.home,
            workspace_id=WORKSPACE, actuation_port=port,
            launch_authority=lambda pin: True, store_admission=lambda key, pin: None,
        )
        self.assertEqual(result.stopped, STOPPED_AUTHORITY_INVALID)
        self.assertEqual(port.launched, [])
        self.assertEqual(self._row_revision_and_lease(), before)

    def test_a_padded_or_foreign_refusal_is_never_actionable(self):
        """`_raw` folded a padded refusal to "" and read it as not-refused (j#97198 F4)."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
            RecoveryPlan,
        )

        padded = RecoveryPlan(
            decision=RecoveryDecision(
                outcome=self.plan.decision.outcome,
                action_id=self.plan.action_id,
                refusal=" denied ",
            ),
            action_id=self.plan.action_id,
        )
        foreign = RecoveryPlan(
            decision=SimpleNamespace(
                outcome=self.plan.decision.outcome,
                action_id=self.plan.action_id,
                refusal="",
            ),
            action_id=self.plan.action_id,
        )
        for label, plan in (("padded refusal", padded), ("foreign decision", foreign)):
            with self.subTest(label=label):
                self.setUp()
                before = self._row_revision_and_lease()
                port = _Port()
                result = self._actuate(port, plan=plan)
                self.assertEqual(result.stopped, STOPPED_NOT_ACTIONABLE)
                self.assertEqual(port.launched, [])
                self.assertEqual(self._row_revision_and_lease(), before)
                self.assertEqual(self._evidence_phase(), "bound")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
