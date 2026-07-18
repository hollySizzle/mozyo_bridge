"""Redmine #13806 R2 — recover-stale revision authority split + zero-effect convergence.

The installed ``0.12.0a10`` binary could neither recover the #13811 stale worker nor converge
the residue it created, because ONE ``--lane-revision`` field was compared to TWO different
live authorities: the preflight ``generation_matches`` gate read it against the live worker
INVENTORY row revision (``0``), while the close-boundary preservation fence read it against the
live lane LIFECYCLE revision (``5``, generation ``1``). No single value satisfied both, so:

- ``--lane-revision 5`` → preflight ``stale_generation`` (row rev ``0`` != ``5``), zero effect;
- ``--lane-revision 0`` → preflight actionable but the close boundary ``preservation_blocked``
  (pin lifecycle ``0`` != live ``5``);

and the a10 run left a durable transaction pinned to mis-bound evidence, stuck at
``replacing_nonself`` with the worker still ``close_owed`` (zero close / launch / send) — a
corrected re-run then tripped the use case's authority-conflict fence.

This pins the correction:

1. the two revisions are DISTINCT authorities — ``--worker-revision`` (preflight, vs the live
   row) and ``--lane-revision`` / ``--lane-generation`` (preservation, vs the live lifecycle) —
   so the #13811 shape converges to actionable → close → fresh launch → redispatch;
2. the closed preservation reason(s) + the comparison axis reach the public / durable outcome;
3. a public ``--supersede`` re-anchors the zero-effect residue to a fresh generation WITHOUT
   raw DB, and ONLY while it has actuated nothing — after any close / launch / send / a foreign
   or in-flight row it keeps its immutable fence, zero-write.

All state lives under isolated homes; the live process mutation is faked (non-scope, j#79485).
The live-composition case wires the REAL adapters against a REAL lane-lifecycle store.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PRESERVE_IDENTITY_MISMATCH,
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    CAS_ACTION_MISMATCH,
    CAS_APPLIED,
    CAS_GENERATION_MISMATCH,
    CAS_NOT_FOUND,
    CAS_UNEXPECTED_STATE,
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
    PHASE_CLAIMED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_PLANNED,
    PHASE_REPLACING_NONSELF,
    transaction_has_zero_actuation_effect,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RECOVERY_COMPLETED,
    RECOVERY_REFUSED,
    RECOVERY_STOPPED,
    RecoveryRequest,
    StaleWorkerRecoveryUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ACTUATION_PRESERVATION_BLOCKED,
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E402,E501
    RECOVER_ACTIONABLE,
    RecoveryObservation,
)

WS = "ws1"
GEN = 7
FIXED = "2026-07-15T12:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"

# The exact stale worker of the #13811 shape.
LANE = "issue_13811_project_gateway_lifecycle_adapter"
ROLE = "worker"
PROVIDER = "claude"
NAME = "wk1"
LOCATOR = "w28:p2"
ACTION_ID = f"recover:{LANE}:{ROLE}:{PROVIDER}:{NAME}:{LOCATOR}"
WK_IDENTITY = (LANE, ROLE, PROVIDER, NAME)


def _decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id="13806", journal_id="81667")


def _continuation() -> ContinuationPointer:
    return ContinuationPointer(
        source="redmine", issue_id="13806", journal_id="81667",
        expected_gate="implementation_request", next_semantic_action="dispatch_once",
    )


def _worker(*, lane_revision: str = "5", lane_generation: str = "1") -> ParticipantPin:
    """The pinned stale worker, carrying its LANE LIFECYCLE evidence (not the row revision)."""
    return ParticipantPin(
        lane_id=LANE, role=ROLE, provider=PROVIDER, assigned_name=NAME,
        old_locator=LOCATOR, is_self=False,
        lane_revision=lane_revision, lane_generation=lane_generation,
    )


# ============================================================================
# 1. Store-level supersede CAS (the zero-effect convergence primitive)
# ============================================================================


class SupersedeStoreTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey(WS, ACTION_ID)

    # -- fixture: position a durable transaction ----------------------------

    def _plan_zero_effect(self, *, worker=None, gen=GEN):
        """Plan a single-worker recovery transaction (the a10 residue's zero-effect shape)."""
        worker = worker if worker is not None else _worker(lane_revision="0", lane_generation="1")
        out = self.store.plan_transaction(
            self.key, action_generation=gen, decision=_decision(),
            continuation=_continuation(), participants=[worker], now=FIXED,
        )
        self.assertTrue(out.applied)
        return worker

    def _drive_to_replacing_nonself(self, *, holder="a10", expiry=FUTURE):
        """planned -> claimed -> replacing_nonself, worker still close_owed (no actuation)."""
        rec = self.store.get(self.key)
        claim = self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder=holder, lease_expires_at=expiry, now=FIXED,
        )
        self.assertTrue(claim.applied)
        for target in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
            rec = self.store.get(self.key)
            out = self.store.transition_phase(
                self.key, expected_revision=rec.revision, expected_action_generation=GEN,
                target=target, holder=holder, now=FIXED,
            )
            self.assertTrue(out.applied, f"{target}: {out}")

    def _release_lease(self, holder="a10"):
        rec = self.store.get(self.key)
        out = self.store.release(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder=holder, now=FIXED,
        )
        self.assertTrue(out.applied)

    def _supersede(self, *, gen=GEN + 1, worker=None, decision=None, continuation=None):
        return self.store.supersede_transaction(
            self.key, new_action_generation=gen,
            decision=decision if decision is not None else _decision(),
            continuation=continuation if continuation is not None else _continuation(),
            participants=[worker if worker is not None else _worker()], now=FIXED,
        )

    # -- the happy convergence ----------------------------------------------

    def test_zero_effect_residue_reanchors_to_new_generation(self):
        # The a10 residue: pinned to lane_revision "0" (mis-bound), stuck at replacing_nonself,
        # worker still close_owed, lease dead.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        self._drive_to_replacing_nonself()
        self._release_lease()
        stuck = self.store.get(self.key)
        self.assertEqual(stuck.phase, PHASE_REPLACING_NONSELF)
        self.assertTrue(transaction_has_zero_actuation_effect(stuck))

        # Supersede to the corrected lifecycle evidence (5, 1) at a higher generation.
        out = self._supersede(gen=GEN + 1, worker=_worker(lane_revision="5", lane_generation="1"))
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)

        after = self.store.get(self.key)
        self.assertEqual(after.action_generation, GEN + 1)
        self.assertEqual(after.phase, PHASE_PLANNED)  # reset to drive afresh
        self.assertEqual(after.lease_holder, "")  # superseded lease never carries forward
        self.assertGreater(after.revision, stuck.revision)  # CAS revision is monotonic
        pin = after.find_participant(WK_IDENTITY)
        self.assertEqual(pin.lane_revision, "5")  # re-anchored to the correct authority
        self.assertEqual(pin.lane_generation, "1")
        self.assertEqual(pin.phase, PARTICIPANT_CLOSE_OWED)

    def test_supersede_resets_created_at_freshness_boundary(self):
        # created_at is the attestation freshness boundary the relaunch verifies the fresh slot
        # against — a new generation must anchor it to its OWN start, never the stale residue's.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        residue = self.store.get(self.key)
        later = "2026-07-15T14:00:00+00:00"
        out = self.store.supersede_transaction(
            self.key, new_action_generation=GEN + 1, decision=_decision(),
            continuation=_continuation(), participants=[_worker(lane_revision="5")], now=later,
        )
        self.assertTrue(out.applied)
        after = self.store.get(self.key)
        self.assertEqual(after.created_at, later)  # reset to the supersede moment
        self.assertNotEqual(after.created_at, residue.created_at)

    def test_supersede_from_planned_and_claimed_zero_effect(self):
        # planned (never claimed) is also zero-effect and re-anchorable.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        out = self._supersede(worker=_worker(lane_revision="5"))
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).find_participant(WK_IDENTITY).lane_revision, "5")

    # -- the immutable fences (zero-write) ----------------------------------

    def test_supersede_refused_after_a_close_happened(self):
        # The worker advanced to launch_owed => the old slot was closed. Never re-anchor.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        self._drive_to_replacing_nonself()
        rec = self.store.get(self.key)
        moved = self.store.transition_participant(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            identity=WK_IDENTITY, target=PARTICIPANT_LAUNCH_OWED, holder="a10", now=FIXED,
        )
        self.assertTrue(moved.applied)
        self._release_lease()
        before = self.store.get(self.key)
        out = self._supersede(worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        # zero-write: the row is byte-identical (still the launch_owed, old evidence)
        after = self.store.get(self.key)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.action_generation, GEN)
        self.assertEqual(after.find_participant(WK_IDENTITY).lane_revision, "0")

    def test_supersede_refused_after_send_phase(self):
        # Drive the worker fully replaced and into draining_continuation (a redispatch send may
        # have fired) — an immutable fence, never re-anchored.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        self._drive_to_replacing_nonself()
        for target in (PARTICIPANT_LAUNCH_OWED, PARTICIPANT_VERIFY_OWED, PARTICIPANT_REPLACED):
            rec = self.store.get(self.key)
            out = self.store.transition_participant(
                self.key, expected_revision=rec.revision, expected_action_generation=GEN,
                identity=WK_IDENTITY, target=target, holder="a10", now=FIXED,
            )
            self.assertTrue(out.applied, f"{target}: {out}")
        rec = self.store.get(self.key)
        drain = self.store.transition_phase(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            target=PHASE_DRAINING_CONTINUATION, holder="a10", now=FIXED,
        )
        self.assertTrue(drain.applied)
        self._release_lease()
        out = self._supersede(worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_supersede_refused_while_a_live_lease_is_held(self):
        # A different authority is still in flight (live lease) — never stomp it.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        self._drive_to_replacing_nonself(holder="live-holder", expiry=FUTURE)  # lease NOT released
        out = self._supersede(worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_expired_lease_is_still_supersedable(self):
        # A dead (expired) lease is not an in-flight authority — the residue converges. The
        # lease is live while the a10 run drove to replacing_nonself (expiry just after FIXED),
        # then dead by the time the owner supersedes (a later ``now``).
        soon = "2026-07-15T12:05:00+00:00"
        later = "2026-07-15T13:00:00+00:00"
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        self._drive_to_replacing_nonself(holder="dead", expiry=soon)
        out = self.store.supersede_transaction(
            self.key, new_action_generation=GEN + 1, decision=_decision(),
            continuation=_continuation(), participants=[_worker(lane_revision="5")], now=later,
        )
        self.assertTrue(out.applied)

    def test_supersede_refused_on_different_decision(self):
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        other = DecisionPointer(source="redmine", issue_id="13806", journal_id="99999")
        out = self._supersede(decision=other, worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ACTION_MISMATCH)

    def test_supersede_refused_on_different_continuation(self):
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        other = ContinuationPointer(
            source="redmine", issue_id="13806", journal_id="81667",
            expected_gate="review_request", next_semantic_action="dispatch_once",
        )
        out = self._supersede(continuation=other, worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ACTION_MISMATCH)

    def test_supersede_refused_on_different_worker_identity(self):
        # Re-anchoring may correct EVIDENCE, never re-target a different worker.
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        foreign = ParticipantPin(
            lane_id=LANE, role=ROLE, provider=PROVIDER, assigned_name="OTHER",
            old_locator=LOCATOR, is_self=False, lane_revision="5", lane_generation="1",
        )
        out = self._supersede(worker=foreign)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ACTION_MISMATCH)

    def test_supersede_requires_strictly_greater_generation(self):
        self._plan_zero_effect(worker=_worker(lane_revision="0"), gen=GEN)
        same = self._supersede(gen=GEN, worker=_worker(lane_revision="5"))
        self.assertFalse(same.applied)
        self.assertEqual(same.reason, CAS_GENERATION_MISMATCH)
        lower = self._supersede(gen=GEN - 1, worker=_worker(lane_revision="5"))
        self.assertFalse(lower.applied)
        self.assertEqual(lower.reason, CAS_GENERATION_MISMATCH)

    def test_supersede_missing_row_is_not_found(self):
        out = self._supersede(worker=_worker(lane_revision="5"))
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_supersede_rejects_nonpositive_and_bool_generation(self):
        self._plan_zero_effect(worker=_worker(lane_revision="0"))
        with self.assertRaises(ValueError):
            self._supersede(gen=0, worker=_worker(lane_revision="5"))
        with self.assertRaises(ValueError):
            self.store.supersede_transaction(
                self.key, new_action_generation=True, decision=_decision(),
                continuation=_continuation(), participants=[_worker()], now=FIXED,
            )


# ============================================================================
# 2. Use-case convergence of the a10 residue (real store, faked live legs)
# ============================================================================


def _all_clear(**overrides) -> RecoveryObservation:
    facts = dict(
        identity_resolved=True, is_standard_sublane_worker=True, issue_lane_matches=True,
        generation_matches=True, not_productive=True, is_stale=True,
        worktree_readable=True, no_authority_conflict=True,
    )
    facts.update(overrides)
    return RecoveryObservation(**facts)


class _FakeActuatorPort:
    """Synthetic ExactGenerationActuatorPort — no live process, no DB."""

    def __init__(self):
        self.pres: dict[tuple, PreservationObservation] = {}
        self._default_pres = PreservationObservation(
            identity_matches=True, attestation_fresh=True
        )
        self.closed: list[tuple] = []

    def observe_old_slot(self, pin: ParticipantPin) -> str:
        return OLD_SLOT_PRESENT

    def observe_preservation(self, pin: ParticipantPin) -> PreservationObservation:
        return self.pres.get(pin.identity, self._default_pres)

    def close_exact_generation(self, pin: ParticipantPin) -> str:
        self.closed.append(pin.identity)
        return CLOSE_DONE

    def launch_action_bound(self, action_id: str, pin: ParticipantPin) -> str:
        return LAUNCH_DONE

    def verify_attestation(self, action_id: str, pin: ParticipantPin) -> str:
        return ATTEST_BOUND


class _FakeRecoveryOps:
    def __init__(self, observation, *, landed_after_send=True, already_landed=False):
        self._observation = observation
        self._landed = already_landed
        self._landed_after_send = landed_after_send
        self.sends: list = []

    def observe_target(self, request) -> RecoveryObservation:
        return self._observation

    def redispatch_gate(self, continuation) -> str:
        self.sends.append(continuation)
        if self._landed_after_send:
            self._landed = True
        return DRAIN_SEND_OK

    def gate_redispatched(self, continuation) -> bool:
        return self._landed


class _ConvergenceCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey(WS, ACTION_ID)
        self.port = _FakeActuatorPort()

    def _seed_a10_residue(self, *, lane_revision="0"):
        """The durable mis-bound residue the installed a10 left: replacing_nonself, worker
        close_owed (zero close/launch/send), lease dead, pinned to the WRONG lifecycle
        evidence."""
        worker = _worker(lane_revision=lane_revision, lane_generation="1")
        self.store.plan_transaction(
            self.key, action_generation=GEN, decision=_decision(),
            continuation=_continuation(), participants=[worker], now=FIXED,
        )
        rec = self.store.get(self.key)
        self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="a10", lease_expires_at=FUTURE, now=FIXED,
        )
        for target in (PHASE_CLAIMED, PHASE_REPLACING_NONSELF):
            rec = self.store.get(self.key)
            self.store.transition_phase(
                self.key, expected_revision=rec.revision, expected_action_generation=GEN,
                target=target, holder="a10", now=FIXED,
            )
        rec = self.store.get(self.key)
        self.store.release(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="a10", now=FIXED,
        )

    def _use_case(self, ops):
        return StaleWorkerRecoveryUseCase(
            self.store, self.port, ops, workspace_id=WS, clock=lambda: FIXED,
        )

    def _request(self, **overrides) -> RecoveryRequest:
        base = dict(
            issue="13806", lane=LANE, role=ROLE, provider=PROVIDER, assigned_name=NAME,
            locator=LOCATOR, journal="81667", action_id=ACTION_ID, action_generation=GEN,
            worker_revision="0", lane_revision="5", lane_generation="1",
            expected_gate="implementation_request", next_semantic_action="dispatch_once",
        )
        base.update(overrides)
        return RecoveryRequest(**base)


class UseCaseConvergenceTests(_ConvergenceCase):
    def test_corrected_rerun_without_supersede_is_authority_conflict(self):
        # The residue is pinned to lane_revision "0"; the corrected re-run pins "5". Without
        # --supersede this is an authority conflict — the immutable fence holds, zero actuation.
        self._seed_a10_residue(lane_revision="0")
        outcome = self._use_case(_FakeRecoveryOps(_all_clear())).run(
            self._request(action_generation=GEN + 1, supersede=False), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("--supersede", outcome.detail)
        self.assertEqual(self.port.closed, [])  # nothing closed

    def test_supersede_converges_residue_to_completed(self):
        self._seed_a10_residue(lane_revision="0")
        ops = _FakeRecoveryOps(_all_clear())
        outcome = self._use_case(ops).run(
            self._request(action_generation=GEN + 1, supersede=True), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertTrue(outcome.converged_supersede)
        self.assertTrue(outcome.closed_old_worker)
        self.assertEqual(len(ops.sends), 1)  # redispatched exactly once
        # the row is at the new generation with corrected evidence, fully driven
        rec = self.store.get(self.key)
        self.assertEqual(rec.action_generation, GEN + 1)
        self.assertEqual(rec.find_participant(WK_IDENTITY).lane_revision, "5")

    def test_supersede_refused_when_residue_already_closed(self):
        # A residue that already closed the old worker (worker at launch_owed) is an immutable
        # fence — --supersede refuses, zero-write.
        self._seed_a10_residue(lane_revision="0")
        rec = self.store.get(self.key)
        # simulate the a10 residue having advanced the worker past close_owed
        self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="a10b", lease_expires_at=FUTURE, now=FIXED,
        )
        rec = self.store.get(self.key)
        self.store.transition_participant(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            identity=WK_IDENTITY, target=PARTICIPANT_LAUNCH_OWED, holder="a10b", now=FIXED,
        )
        rec = self.store.get(self.key)
        self.store.release(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="a10b", now=FIXED,
        )
        outcome = self._use_case(_FakeRecoveryOps(_all_clear())).run(
            self._request(action_generation=GEN + 1, supersede=True), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("supersede refused", outcome.detail)
        self.assertEqual(self.port.closed, [])

    def test_supersede_requires_higher_generation(self):
        # --supersede at the SAME generation cannot re-anchor (a monotonic, owner-approved bump).
        self._seed_a10_residue(lane_revision="0")
        outcome = self._use_case(_FakeRecoveryOps(_all_clear())).run(
            self._request(action_generation=GEN, supersede=True), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_REFUSED)
        self.assertIn("supersede refused", outcome.detail)

    def test_fresh_key_ignores_supersede_and_drives_normally(self):
        # No residue: --supersede is a no-op; a fresh plan drives straight through.
        outcome = self._use_case(_FakeRecoveryOps(_all_clear())).run(
            self._request(supersede=True), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_COMPLETED)
        self.assertFalse(outcome.converged_supersede)  # never re-anchored a stuck row

    def test_idempotent_replay_after_supersede_does_not_reclose(self):
        self._seed_a10_residue(lane_revision="0")
        req = self._request(action_generation=GEN + 1, supersede=True)
        first = self._use_case(_FakeRecoveryOps(_all_clear())).run(req, execute=True)
        self.assertEqual(first.status, RECOVERY_COMPLETED)
        closes_after_first = list(self.port.closed)
        # a replay at the corrected generation resumes the completed transaction: the gate has
        # already landed (the durable oracle confirms it), so ZERO re-close and ZERO re-send.
        replay_ops = _FakeRecoveryOps(_all_clear(), already_landed=True)
        second = self._use_case(replay_ops).run(req, execute=True)
        self.assertEqual(second.status, RECOVERY_COMPLETED)
        self.assertEqual(self.port.closed, closes_after_first)  # never closed twice
        self.assertEqual(replay_ops.sends, [])  # never re-sent


# ============================================================================
# 3. Preservation reason(s) + comparison axis reach the public outcome
# ============================================================================


class PreservationSurfaceTests(_ConvergenceCase):
    def test_preservation_block_surfaces_reason_and_axis(self):
        # A fresh plan whose close boundary observes a lane-lifecycle identity mismatch: the
        # public outcome must name the CLOSED reason (identity_mismatch) and the comparison axis
        # (never a generic preservation_blocked).
        self.port.pres[WK_IDENTITY] = PreservationObservation(
            identity_matches=False, attestation_fresh=True,
            detail="lane_lifecycle_revision observed='5' pinned='0'",
        )
        outcome = self._use_case(_FakeRecoveryOps(_all_clear())).run(
            self._request(), execute=True
        )
        self.assertEqual(outcome.status, RECOVERY_STOPPED)
        self.assertEqual(outcome.recovery_status, ACTUATION_PRESERVATION_BLOCKED)
        self.assertIn(PRESERVE_IDENTITY_MISMATCH, outcome.preservation_reasons)
        self.assertIn("lane_lifecycle_revision", outcome.detail)
        self.assertIn(PRESERVE_IDENTITY_MISMATCH, outcome.as_payload()["preservation_reasons"])


# ============================================================================
# 4. Live production adapter composition — the #13811 shape against REAL stores
# ============================================================================

import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live  # noqa: E402,E501
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    LaneLifecycleKey,
    LaneLifecycleStore,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DecisionPointer as LifecycleDecisionPointer,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

# The live composition uses the repo's real worker provider (claude): herdr's assigned-name
# identity carries the provider as its ``role``.
CWS = "wsC"
CLANE = "issue_13811_project_gateway_lifecycle_adapter"
CROLE = "claude"
CLOCATOR = "w28:p2N"
CNAME = encode_assigned_name(CWS, CROLE, CLANE)


class LiveCompositionSplitTests(unittest.TestCase):
    """The #13811 shape (worker ROW revision 0 vs lane LIFECYCLE revision 5, generation 1)
    through the REAL live adapters + a REAL lane-lifecycle store. Proves the two revisions are
    distinct authorities read from two distinct live sources — so no single conflated field can
    satisfy both the preflight generation gate and the close-boundary preservation fence."""

    def setUp(self):
        self._orig_rows = live.list_herdr_agent_rows
        self._orig_ws = live.repo_scope_workspace_id
        live.repo_scope_workspace_id = lambda root: CWS
        # A REAL lane-lifecycle store seeded to the #13811 shape: revision 5, generation 1.
        self.lifecycle_home = Path(tempfile.mkdtemp())
        key = LaneLifecycleKey(CWS, CLANE)
        dec = LifecycleDecisionPointer(source="redmine", issue_id="13811", journal_id="81667")
        LaneDeclarationStore(home=self.lifecycle_home).declare_lane(
            key, decision=dec, issue_id="13811"
        )
        store = LaneLifecycleStore(home=self.lifecycle_home)
        cur = DISPOSITION_ACTIVE
        for target in (
            DISPOSITION_HIBERNATED, DISPOSITION_ACTIVE,
            DISPOSITION_HIBERNATED, DISPOSITION_ACTIVE,
        ):
            rec = store.get(key)
            out = store.transition_disposition(
                key, expected_disposition=cur, expected_revision=rec.revision,
                target=target, decision=dec,
            )
            self.assertTrue(out.applied, out.reason)
            cur = target
        seeded = store.get(key)
        self.assertEqual((seeded.revision, seeded.lane_generation), (5, 1))

    def tearDown(self):
        live.list_herdr_agent_rows = self._orig_rows
        live.repo_scope_workspace_id = self._orig_ws

    def _row(self, **overrides):
        row = {
            "name": CNAME, "pane_id": CLOCATOR, "agent": "", "status": "unknown",
            "revision": 0,  # the #13811 worker ROW revision (NOT the lane lifecycle revision)
            "foreground_cwd": str(ROOT),
        }
        row.update(overrides)
        return row

    def _request(self, **overrides) -> RecoveryRequest:
        base = dict(
            issue="13811", lane=CLANE, role=CROLE, provider=CROLE, assigned_name=CNAME,
            locator=CLOCATOR, journal="81667", action_id="", action_generation=8,
            worker_revision="0", lane_revision="5", lane_generation="1",
            expected_gate="implementation_request", next_semantic_action="dispatch_once",
        )
        base.update(overrides)
        return RecoveryRequest(**base)

    def _ops(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        return live.LiveStaleWorkerRecoveryOps(repo_root=ROOT, request=self._request())

    def _port(self, rows):
        live.list_herdr_agent_rows = lambda env: rows
        store = ReplacementTransactionStore(home=Path(tempfile.mkdtemp()))
        return live.LiveRecoveryActuatorPort(
            repo_root=ROOT, request=self._request(),
            store=store, key=ReplacementTransactionKey(CWS, "recover:k"),
            lifecycle_home=self.lifecycle_home,
        )

    def _pin(self, *, lane_revision="5", lane_generation="1") -> ParticipantPin:
        return ParticipantPin(
            lane_id=CLANE, role=CROLE, provider=CROLE, assigned_name=CNAME,
            old_locator=CLOCATOR, lane_revision=lane_revision, lane_generation=lane_generation,
        )

    # -- the split: preflight reads the ROW revision ------------------------

    def test_preflight_actionable_on_worker_revision_regardless_of_lane_revision(self):
        # worker_revision "0" matches the row (0); lane_revision "5" is the LIFECYCLE authority
        # and must NOT drive the preflight generation gate.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
            decide_recovery,
        )

        obs = self._ops([self._row()]).observe_target(
            self._request(worker_revision="0", lane_revision="5", lane_generation="1")
        )
        self.assertEqual(decide_recovery(obs), RECOVER_ACTIONABLE)

    def test_preflight_stale_when_worker_revision_set_to_the_lifecycle_value(self):
        # The a10 failure mode: pinning the LIFECYCLE revision (5) where the WORKER revision (0)
        # belongs makes the preflight see a stale generation (row 0 != 5). The split is what
        # lets the owner pin the two independently.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
            RECOVER_BLOCK_STALE_GENERATION,
            decide_recovery,
        )

        obs = self._ops([self._row()]).observe_target(self._request(worker_revision="5"))
        self.assertEqual(decide_recovery(obs), RECOVER_BLOCK_STALE_GENERATION)

    # -- the split: preservation reads the LANE LIFECYCLE -------------------

    def test_preservation_passes_when_pin_matches_live_lifecycle(self):
        obs = self._port([self._row()]).observe_preservation(
            self._pin(lane_revision="5", lane_generation="1")
        )
        self.assertTrue(obs.identity_matches)

    def test_preservation_blocks_and_names_axis_when_pin_lifecycle_wrong(self):
        # The a10 failure mode's other horn: pinning the WORKER revision (0) where the LIFECYCLE
        # revision (5) belongs blocks the close, and the detail names the diverging axis + the
        # observed-vs-pinned values (never a generic block, never any secret).
        obs = self._port([self._row()]).observe_preservation(
            self._pin(lane_revision="0", lane_generation="1")
        )
        self.assertFalse(obs.identity_matches)
        self.assertIn("lane_lifecycle_revision", obs.detail)
        self.assertIn("observed='5'", obs.detail)
        self.assertIn("pinned='0'", obs.detail)

    def test_single_conflated_field_cannot_satisfy_both_fences(self):
        # The crux of the bug: any single value V for a conflated field either fails the
        # preflight (V != row 0) or the preservation fence (V != lifecycle 5). Only the split —
        # worker_revision 0 AND lane_revision 5 — satisfies both at once.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
            decide_recovery,
        )

        for conflated in ("0", "5"):
            preflight = decide_recovery(
                self._ops([self._row()]).observe_target(
                    self._request(worker_revision=conflated)
                )
            )
            preservation_ok = self._port([self._row()]).observe_preservation(
                self._pin(lane_revision=conflated, lane_generation="1")
            ).identity_matches
            # never both green for one shared value
            self.assertFalse(
                preflight == RECOVER_ACTIONABLE and preservation_ok,
                f"conflated value {conflated!r} unexpectedly satisfied both fences",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
