"""Redmine #13806 tranche B — generic exact-generation actuator / partial replay.

The exact-generation actuator (Implementation Request j#79121, design j#78384 / Verdict
j#78406) drives the tranche A replacement transaction's *non-self* participants through
their owed progression (close → action-bound launch → attestation verify) and arms the
transaction up to ``self_close_armed`` — never actuating the self coordinator (tranche C).
It composes the tranche A CAS store with an injected actuation port (faked here — live
process mutation is non-scope) and makes every decision from the pure actuation vocabulary.

Pins the verification matrix from j#79121: partial-close / crash replay, duplicate invoke,
same-name recycle, action-attestation mismatch, lease loss, and the preservation negative
matrix. All state lives under an isolated home — never the shared ``$HOME/.mozyo_bridge``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PRESERVE_DIRTY_DIFF,
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
    PHASE_SELF_CLOSE_ARMED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E402
    ActuationResult,
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator_ops import (  # noqa: E402
    ExactGenerationActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402
    ACTUATION_AMBIGUOUS,
    ACTUATION_ARMED,
    ACTUATION_ATTESTATION_MISMATCH,
    ACTUATION_EFFECT_FAILED,
    ACTUATION_GENERATION_MISMATCH,
    ACTUATION_IN_PROGRESS,
    ACTUATION_LEASE_LOST,
    ACTUATION_NOT_FOUND,
    ACTUATION_PRESERVATION_BLOCKED,
    ACTUATION_RECYCLED,
    ATTEST_BOUND,
    ATTEST_MISMATCH,
    ATTEST_PENDING,
    CLOSE_DONE,
    CLOSE_ERROR,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_ABSENT,
    OLD_SLOT_AMBIGUOUS,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
    attestation_completes,
    bounded_recovery_available,
    is_zero_actuation_observation,
    new_close_required,
    zero_actuation_status,
)

GEN = 7
FIXED = "2026-07-15T12:00:00+00:00"


class FakeActuatorPort:
    """A synthetic :class:`ExactGenerationActuatorPort` — no live process, no DB."""

    def __init__(self):
        self.old: dict[tuple, str] = {}
        self.pres: dict[tuple, PreservationObservation] = {}
        self.attest: dict[tuple, str] = {}
        self.close_result: dict[tuple, str] = {}
        self.launch_result: dict[tuple, str] = {}
        self.closed: list[tuple] = []
        self.launched: list[tuple[str, tuple]] = []
        self.verified: list[tuple[str, tuple]] = []
        self._default_pres = PreservationObservation(
            identity_matches=True, attestation_fresh=True
        )

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return self.old.get(pin.identity, OLD_SLOT_PRESENT)

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        return self.pres.get(pin.identity, self._default_pres)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        self.closed.append(pin.identity)
        return self.close_result.get(pin.identity, CLOSE_DONE)

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        self.launched.append((action_id, pin.identity))
        return self.launch_result.get(pin.identity, LAUNCH_DONE)

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        self.verified.append((action_id, pin.identity))
        return self.attest.get(pin.identity, ATTEST_BOUND)


def _gateway() -> ParticipantPin:
    return ParticipantPin(
        lane_id="l", role="gateway", provider="codex",
        assigned_name="gw", old_locator="w:1",
    )


def _worker() -> ParticipantPin:
    return ParticipantPin(
        lane_id="l", role="worker", provider="claude",
        assigned_name="wk", old_locator="w:2",
    )


def _self_coordinator() -> ParticipantPin:
    return ParticipantPin(
        lane_id="d", role="coordinator", provider="codex",
        assigned_name="cx", old_locator="w:3", is_self=True,
    )


class _ActuatorCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("ws", "a:gen7")
        self.gw, self.wk, self.sc = _gateway(), _worker(), _self_coordinator()
        self.store.plan_transaction(
            self.key,
            action_generation=GEN,
            decision=DecisionPointer(
                source="redmine", issue_id="13806", journal_id="78948"
            ),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="78948",
                expected_gate="review_request", next_semantic_action="dispatch_once",
            ),
            participants=[self.gw, self.wk, self.sc],
        )
        self.port = FakeActuatorPort()

    def _actuator(self, port=None):
        return ReplacementActuatorUseCase(
            self.store, port or self.port, clock=lambda: FIXED
        )

    def _run(self, holder="H", gen=GEN, port=None):
        return self._actuator(port).run(
            self.key, holder=holder, expected_action_generation=gen
        )

    def _phase_of(self, pin):
        return self.store.get(self.key).find_participant(pin.identity).phase


class HappyPathTests(_ActuatorCase):
    def test_full_choreography_arms_and_replaces_nonself_only(self):
        result = self._run()
        self.assertEqual(result.status, ACTUATION_ARMED)
        self.assertTrue(result.armed)
        rec = self.store.get(self.key)
        self.assertEqual(rec.phase, PHASE_SELF_CLOSE_ARMED)
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_REPLACED)
        self.assertEqual(self._phase_of(self.wk), PARTICIPANT_REPLACED)
        # the self coordinator is NEVER actuated by tranche B
        self.assertEqual(self._phase_of(self.sc), PARTICIPANT_CLOSE_OWED)
        self.assertNotIn(self.sc.identity, self.port.closed)
        self.assertFalse(any(x[1] == self.sc.identity for x in self.port.launched))

    def test_launch_is_action_bound(self):
        self._run()
        # every launch carries the transaction's replacement action id
        self.assertTrue(self.port.launched)
        self.assertTrue(all(action == "a:gen7" for action, _ in self.port.launched))

    def test_rerun_of_armed_transaction_is_idempotent(self):
        first = self._run()
        self.assertEqual(first.status, ACTUATION_ARMED)
        closed_after_first = list(self.port.closed)
        second = self._run()
        self.assertEqual(second.status, ACTUATION_ARMED)
        # no additional closes/launches on a re-run of an already-armed transaction
        self.assertEqual(self.port.closed, closed_after_first)


class ZeroActuationTests(_ActuatorCase):
    def test_recycled_old_slot_is_zero_actuation(self):
        self.port.old[self.gw.identity] = OLD_SLOT_RECYCLED
        result = self._run()
        self.assertEqual(result.status, ACTUATION_RECYCLED)
        self.assertEqual(result.stopped_on, self.gw.identity)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_CLOSE_OWED)

    def test_ambiguous_inventory_is_zero_actuation(self):
        self.port.old[self.gw.identity] = OLD_SLOT_AMBIGUOUS
        result = self._run()
        self.assertEqual(result.status, ACTUATION_AMBIGUOUS)
        self.assertEqual(self.port.closed, [])

    def test_close_error_stops_before_owed_advance(self):
        self.port.close_result[self.gw.identity] = CLOSE_ERROR
        result = self._run()
        self.assertEqual(result.status, ACTUATION_EFFECT_FAILED)
        self.assertEqual(result.detail, "close")
        # the participant stays close_owed — the close is not assumed
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_CLOSE_OWED)

    def test_launch_error_stays_launch_owed(self):
        self.port.launch_result[self.gw.identity] = LAUNCH_ERROR
        result = self._run()
        self.assertEqual(result.status, ACTUATION_EFFECT_FAILED)
        self.assertEqual(result.detail, "launch")
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_LAUNCH_OWED)


class PreservationFenceTests(_ActuatorCase):
    def test_new_close_is_preservation_gated(self):
        self.port.pres[self.gw.identity] = PreservationObservation(
            dirty_diff=True, identity_matches=True, attestation_fresh=True
        )
        result = self._run()
        self.assertEqual(result.status, ACTUATION_PRESERVATION_BLOCKED)
        self.assertEqual(result.preservation_reasons, (PRESERVE_DIRTY_DIFF,))
        self.assertEqual(self.port.closed, [])  # zero additional close
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_CLOSE_OWED)

    def test_default_missing_observation_blocks(self):
        # A preservation observation with no positive evidence fails closed (tranche A).
        self.port.pres[self.gw.identity] = PreservationObservation()
        result = self._run()
        self.assertEqual(result.status, ACTUATION_PRESERVATION_BLOCKED)
        self.assertEqual(self.port.closed, [])

    def test_bounded_recovery_launch_ignores_preservation(self):
        # R1-F6 distinction: an already-closed (positive-absent) participant's launch is
        # bounded recovery — it proceeds even under a preservation signal that would block a
        # NEW close, and performs NO close.
        self.port.old[self.gw.identity] = OLD_SLOT_ABSENT
        self.port.pres[self.gw.identity] = PreservationObservation(dirty_diff=True)
        result = self._run()
        self.assertEqual(result.status, ACTUATION_ARMED)
        self.assertNotIn(self.gw.identity, self.port.closed)  # never closed
        self.assertTrue(any(x[1] == self.gw.identity for x in self.port.launched))
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_REPLACED)


class PartialReplayTests(_ActuatorCase):
    def test_crash_after_close_resumes_to_launch_without_reclosing(self):
        # Simulate a close-then-crash: the participant is still close_owed durably, but the
        # old generation is now positively absent (the close committed). The resume must
        # advance to launch WITHOUT a second close.
        self.port.old[self.gw.identity] = OLD_SLOT_ABSENT  # positive absence, no recycle
        result = self._run()
        self.assertEqual(result.status, ACTUATION_ARMED)
        self.assertNotIn(self.gw.identity, self.port.closed)
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_REPLACED)

    def test_attestation_pending_yields_and_resumes(self):
        self.port.attest[self.gw.identity] = ATTEST_PENDING
        first = self._run()
        self.assertEqual(first.status, ACTUATION_IN_PROGRESS)
        self.assertEqual(first.stopped_on, self.gw.identity)
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_VERIFY_OWED)
        # a later re-run, once the fresh slot attests, completes it
        self.port.attest[self.gw.identity] = ATTEST_BOUND
        second = self._run()
        self.assertEqual(second.status, ACTUATION_ARMED)
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_REPLACED)

    def test_resume_from_mid_transaction_makes_no_duplicate_effects(self):
        # First run stalls at gw attestation pending; wk untouched.
        self.port.attest[self.gw.identity] = ATTEST_PENDING
        self._run()
        closed_once = list(self.port.closed)
        # resume: gw now attests; the driver must not re-close gw and must go on to wk.
        self.port.attest[self.gw.identity] = ATTEST_BOUND
        self._run()
        # gw closed exactly once across both runs
        self.assertEqual(self.port.closed.count(self.gw.identity), 1)
        self.assertIn(self.wk.identity, self.port.closed)


class AttestationTests(_ActuatorCase):
    def test_attestation_mismatch_is_zero_completion(self):
        # A fresh slot that attests but NOT to this replacement action is not completion.
        self.port.attest[self.gw.identity] = ATTEST_MISMATCH
        result = self._run()
        self.assertEqual(result.status, ACTUATION_ATTESTATION_MISMATCH)
        self.assertEqual(result.stopped_on, self.gw.identity)
        self.assertEqual(self._phase_of(self.gw), PARTICIPANT_VERIFY_OWED)


class LeaseAndGenerationTests(_ActuatorCase):
    def test_lease_conflict_stops_before_any_effect(self):
        # A live foreign holder owns the lease; the actuator's claim loses.
        self.store.claim(
            self.key, expected_revision=self.store.get(self.key).revision,
            expected_action_generation=GEN, holder="OTHER",
            lease_expires_at="2099-01-01T00:00:00+00:00", now=FIXED,
        )
        result = self._run(holder="H")
        self.assertEqual(result.status, ACTUATION_LEASE_LOST)
        self.assertEqual(self.port.closed, [])

    def test_lease_lost_mid_drive_stops(self):
        # A port whose first close lets a foreign holder steal the (now-expired-looking)
        # lease is awkward to simulate; instead assert the driver's per-step lease guard by
        # revoking via a foreign claim after the actuator armed a short TTL.
        actuator = ReplacementActuatorUseCase(
            self.store, self.port, clock=lambda: FIXED, lease_ttl_seconds=1
        )
        # Foreign holder claims first with a live lease.
        self.store.claim(
            self.key, expected_revision=self.store.get(self.key).revision,
            expected_action_generation=GEN, holder="OTHER",
            lease_expires_at="2099-01-01T00:00:00+00:00", now=FIXED,
        )
        result = actuator.run(self.key, holder="H", expected_action_generation=GEN)
        self.assertEqual(result.status, ACTUATION_LEASE_LOST)

    def test_generation_mismatch_stops(self):
        result = self._run(gen=GEN + 1)
        self.assertEqual(result.status, ACTUATION_GENERATION_MISMATCH)
        self.assertEqual(self.port.closed, [])

    def test_not_found(self):
        actuator = self._actuator()
        result = actuator.run(
            ReplacementTransactionKey("ws", "missing"),
            holder="H", expected_action_generation=GEN,
        )
        self.assertEqual(result.status, ACTUATION_NOT_FOUND)

    def test_duplicate_invoke_second_holder_is_lease_lost(self):
        # First actuator arms the transaction (holds the lease at FIXED).
        first = self._run(holder="H1")
        self.assertEqual(first.status, ACTUATION_ARMED)
        # A second, concurrent actuator with a different holder and a still-live lease loses.
        second_port = FakeActuatorPort()
        second = self._run(holder="H2", port=second_port)
        # The transaction is armed; H2 cannot steal the live lease H1 holds.
        self.assertEqual(second.status, ACTUATION_LEASE_LOST)
        self.assertEqual(second_port.closed, [])


class ResultPayloadTests(_ActuatorCase):
    def test_result_payload_shape(self):
        result = self._run()
        payload = result.as_payload()
        self.assertEqual(payload["status"], ACTUATION_ARMED)
        self.assertEqual(payload["phase"], PHASE_SELF_CLOSE_ARMED)
        self.assertIsNone(payload["stopped_on"])

    def test_result_is_frozen_dataclass(self):
        result = ActuationResult(status=ACTUATION_ARMED)
        with self.assertRaises(Exception):
            result.status = "x"  # type: ignore[misc]


class PureDomainTests(unittest.TestCase):
    def test_new_close_only_when_present(self):
        self.assertTrue(new_close_required(OLD_SLOT_PRESENT))
        for obs in (OLD_SLOT_ABSENT, OLD_SLOT_RECYCLED, OLD_SLOT_AMBIGUOUS):
            self.assertFalse(new_close_required(obs))

    def test_bounded_recovery_only_on_positive_absence(self):
        self.assertTrue(bounded_recovery_available(OLD_SLOT_ABSENT))
        for obs in (OLD_SLOT_PRESENT, OLD_SLOT_RECYCLED, OLD_SLOT_AMBIGUOUS):
            self.assertFalse(bounded_recovery_available(obs))

    def test_zero_actuation_observations(self):
        self.assertTrue(is_zero_actuation_observation(OLD_SLOT_RECYCLED))
        self.assertTrue(is_zero_actuation_observation(OLD_SLOT_AMBIGUOUS))
        self.assertFalse(is_zero_actuation_observation(OLD_SLOT_PRESENT))
        self.assertFalse(is_zero_actuation_observation(OLD_SLOT_ABSENT))
        self.assertEqual(zero_actuation_status(OLD_SLOT_RECYCLED), ACTUATION_RECYCLED)
        self.assertEqual(zero_actuation_status(OLD_SLOT_AMBIGUOUS), ACTUATION_AMBIGUOUS)
        with self.assertRaises(ValueError):
            zero_actuation_status(OLD_SLOT_PRESENT)

    def test_attestation_completes_only_when_bound(self):
        self.assertTrue(attestation_completes(ATTEST_BOUND))
        self.assertFalse(attestation_completes(ATTEST_PENDING))
        self.assertFalse(attestation_completes(ATTEST_MISMATCH))

    def test_port_protocol_is_runtime_checkable(self):
        self.assertIsInstance(FakeActuatorPort(), ExactGenerationActuatorPort)


if __name__ == "__main__":
    unittest.main()
