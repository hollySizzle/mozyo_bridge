"""Redmine #13806 tranche A — atomic self-replacement transaction core.

The "1 action generation = 1 durable replacement transaction" substrate (Design Answer
j#78384, Coordinator Verdict j#78406, Implementation Request j#78948, R1 review j#79000 /
verdicts j#79007): the session / workspace-scoped replacement transaction component that
binds several participants plus a post-self-close continuation into one owner-approved
generation, WITHOUT pushing the default coordinator into an issue lane's #13810 lifecycle
row. Pins:

- the pure model: the transaction phase DAG, the participant owed progression, the
  cross-axis ordering guards (R1-F1), the continuation-pointer / participant-manifest
  codecs and their fail-closed decoders;
- the store: plan (pristine-only idempotent re-plan, R1-F5), the lease
  (claim/renew/release with conflict / expiry / epoch / **live-holder renew**, R1-F4), the
  phase + participant CAS guarded on exact revision + live lease ownership + immutable
  action generation (R1-F2) + cross-axis ordering (R1-F1);
- the schema: native-component registration, the exact-shape classifier, and the
  downgrade fail-closed guard on BOTH the write and read paths (R1-F3);
- the preservation planner: the pure, additive, fail-closed close fence.

All state lives under an isolated home — never the shared ``$HOME/.mozyo_bridge``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_GENERATION_MISMATCH,
    CAS_LEASE_CONFLICT,
    CAS_LEASE_NOT_HELD,
    CAS_NOT_FOUND,
    CAS_PARTICIPANT_NOT_FOUND,
    CAS_STALE_REVISION,
    ReplacementTransactionError,
    ReplacementTransactionStore,
    load_replacement_transactions,
    load_replacement_transactions_readonly,
    replacement_transaction_path,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_LAUNCH_OWED,
    PARTICIPANT_REPLACED,
    PARTICIPANT_VERIFY_OWED,
    PARTICIPANTS_VERSION,
    PHASE_AWAITING_SELF_TURN_END,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_FRESH_COORDINATOR_CLAIMED,
    PHASE_PLANNED,
    PHASE_REPLACING_NONSELF,
    PHASE_SELF_CLOSE_ARMED,
    ContinuationPointer,
    ContinuationPointerError,
    DecisionPointer,
    ParticipantPin,
    ParticipantPinError,
    ReplacementTransactionKey,
    decode_participants,
    encode_participants,
    participant_actuation_phase_allowed,
    participant_transition_allowed,
    transaction_phase_prerequisite_met,
    transaction_transition_allowed,
    validate_participants,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (  # noqa: E402
    READONLY_COMPONENT_ABSENT,
    READONLY_COMPONENT_RECOGNIZED,
    READONLY_COMPONENT_UNSUPPORTED,
    REPLACEMENT_TRANSACTION_COMPONENT,
    REPLACEMENT_TRANSACTION_RECOVERY_POLICY,
    REPLACEMENT_TRANSACTION_SCHEMA_VERSION,
    TABLE as REPLACEMENT_TABLE,
    ensure_replacement_transaction_schema,
    readonly_component_status,
)
from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PRESERVATION_REASONS,
    PRESERVE_ATTESTATION_MISSING,
    PRESERVE_DIRTY_DIFF,
    PRESERVE_IDENTITY_MISMATCH,
    PRESERVE_PENDING_APPROVAL,
    PRESERVE_RUNNING_PROCESS,
    PRESERVE_UNRECORDED_JOURNAL,
    PreservationObservation,
    assess_preservation,
    identity_observation_for,
)

FUTURE = "2099-01-01T00:00:00+00:00"
GEN = 7  # the action generation used across the store cases


def _decision(issue: str = "13806", journal: str = "78948") -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _continuation(issue: str = "13806", journal: str = "78948") -> ContinuationPointer:
    return ContinuationPointer(
        source="redmine",
        issue_id=issue,
        journal_id=journal,
        expected_gate="review_request",
        next_semantic_action="dispatch_standard_once",
    )


def _gateway() -> ParticipantPin:
    return ParticipantPin(
        lane_id="lane_a", role="gateway", provider="codex",
        assigned_name="gw1", old_locator="w28:p1",
    )


def _worker() -> ParticipantPin:
    return ParticipantPin(
        lane_id="lane_a", role="worker", provider="claude",
        assigned_name="wk1", old_locator="w28:p2",
        lane_revision="5", lane_generation="2",
    )


def _self_coordinator() -> ParticipantPin:
    return ParticipantPin(
        lane_id="default", role="coordinator", provider="codex",
        assigned_name="cx0", old_locator="w25:p3", is_self=True,
    )


class ModelEdgeTests(unittest.TestCase):
    def test_transaction_dag_is_linear(self):
        self.assertTrue(transaction_transition_allowed(PHASE_PLANNED, PHASE_CLAIMED))
        self.assertTrue(
            transaction_transition_allowed(PHASE_CLAIMED, PHASE_REPLACING_NONSELF)
        )
        self.assertTrue(
            transaction_transition_allowed(
                PHASE_DRAINING_CONTINUATION, PHASE_COMPLETED
            )
        )
        self.assertFalse(
            transaction_transition_allowed(PHASE_PLANNED, PHASE_REPLACING_NONSELF)
        )
        self.assertFalse(transaction_transition_allowed(PHASE_CLAIMED, PHASE_CLAIMED))
        self.assertFalse(transaction_transition_allowed(PHASE_COMPLETED, PHASE_PLANNED))

    def test_participant_progression_with_retry_self_loops(self):
        self.assertTrue(
            participant_transition_allowed(
                PARTICIPANT_CLOSE_OWED, PARTICIPANT_LAUNCH_OWED
            )
        )
        self.assertTrue(
            participant_transition_allowed(
                PARTICIPANT_LAUNCH_OWED, PARTICIPANT_LAUNCH_OWED
            )
        )
        self.assertTrue(
            participant_transition_allowed(
                PARTICIPANT_VERIFY_OWED, PARTICIPANT_REPLACED
            )
        )
        self.assertFalse(
            participant_transition_allowed(
                PARTICIPANT_CLOSE_OWED, PARTICIPANT_CLOSE_OWED
            )
        )
        self.assertFalse(
            participant_transition_allowed(
                PARTICIPANT_CLOSE_OWED, PARTICIPANT_VERIFY_OWED
            )
        )
        self.assertFalse(
            participant_transition_allowed(PARTICIPANT_REPLACED, PARTICIPANT_LAUNCH_OWED)
        )

    def test_cross_axis_phase_prerequisite(self):
        # -> awaiting_self_turn_end requires all NON-self replaced; -> completed requires ALL.
        gw = _gateway()
        wk = _worker()
        sc = _self_coordinator()
        all_close = (gw, wk, sc)
        self.assertFalse(
            transaction_phase_prerequisite_met(all_close, PHASE_AWAITING_SELF_TURN_END)
        )
        nonself_done = (
            gw.with_phase(PARTICIPANT_REPLACED),
            wk.with_phase(PARTICIPANT_REPLACED),
            sc,  # self still close_owed
        )
        self.assertTrue(
            transaction_phase_prerequisite_met(
                nonself_done, PHASE_AWAITING_SELF_TURN_END
            )
        )
        # self still un-replaced -> cannot complete
        self.assertFalse(
            transaction_phase_prerequisite_met(nonself_done, PHASE_COMPLETED)
        )
        all_done = tuple(p.with_phase(PARTICIPANT_REPLACED) for p in all_close)
        self.assertTrue(transaction_phase_prerequisite_met(all_done, PHASE_COMPLETED))
        # An edge with no participant prerequisite is always met.
        self.assertTrue(
            transaction_phase_prerequisite_met(all_close, PHASE_REPLACING_NONSELF)
        )

    def test_cross_axis_actuation_gate(self):
        # non-self actuated only in replacing_nonself; self only from self_close_armed on.
        self.assertTrue(
            participant_actuation_phase_allowed(False, PHASE_REPLACING_NONSELF)
        )
        self.assertFalse(participant_actuation_phase_allowed(False, PHASE_CLAIMED))
        self.assertFalse(
            participant_actuation_phase_allowed(False, PHASE_SELF_CLOSE_ARMED)
        )
        self.assertTrue(
            participant_actuation_phase_allowed(True, PHASE_SELF_CLOSE_ARMED)
        )
        self.assertTrue(
            participant_actuation_phase_allowed(True, PHASE_DRAINING_CONTINUATION)
        )
        self.assertFalse(
            participant_actuation_phase_allowed(True, PHASE_REPLACING_NONSELF)
        )


class ContinuationPointerTests(unittest.TestCase):
    def test_requires_readable_anchor_and_closed_tokens(self):
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="0", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            )
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="asana", issue_id="1", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            )
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="1", journal_id="1",
                expected_gate="", next_semantic_action="n",
            )
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="1", journal_id="1",
                expected_gate="g", next_semantic_action="",
            )

    def test_round_trip_payload(self):
        c = _continuation()
        self.assertEqual(c.issue_id, "13806")
        self.assertEqual(c.expected_gate, "review_request")
        self.assertEqual(c.as_payload()["next_semantic_action"], "dispatch_standard_once")


class ParticipantManifestTests(unittest.TestCase):
    def test_pin_requires_full_identity(self):
        with self.assertRaises(ParticipantPinError):
            ParticipantPin(
                lane_id="l", role="", provider="codex",
                assigned_name="n", old_locator="w1:p1",
            )

    def test_validate_rejects_empty_duplicate_and_multiple_self(self):
        with self.assertRaises(ParticipantPinError):
            validate_participants(())
        dup = _gateway()
        with self.assertRaises(ParticipantPinError):
            validate_participants([dup, _gateway()])
        s1 = _self_coordinator()
        s2 = ParticipantPin(
            lane_id="other", role="coordinator", provider="claude",
            assigned_name="cl0", old_locator="w25:p2", is_self=True,
        )
        with self.assertRaises(ParticipantPinError):
            validate_participants([s1, s2])

    def test_encode_decode_round_trip_is_deterministic(self):
        pins = validate_participants([_worker(), _gateway(), _self_coordinator()])
        encoded = encode_participants(pins)
        self.assertEqual(encoded, encode_participants(decode_participants(encoded)))
        back = decode_participants(encoded)
        self.assertEqual(len(back), 3)
        wk = next(p for p in back if p.role == "worker")
        self.assertEqual(wk.lane_revision, "5")
        self.assertEqual(wk.lane_generation, "2")
        self.assertEqual(wk.phase, PARTICIPANT_CLOSE_OWED)

    def test_decode_fail_closed_on_unknown_version_and_shapes(self):
        self.assertEqual(decode_participants(""), ())
        with self.assertRaises(ParticipantPinError):
            decode_participants("{not json")
        with self.assertRaises(ParticipantPinError):
            decode_participants('["not", "an", "envelope"]')
        with self.assertRaises(ParticipantPinError):
            decode_participants(
                '{"version": %d, "participants": []}' % (PARTICIPANTS_VERSION + 1)
            )
        with self.assertRaises(ParticipantPinError):
            decode_participants('{"version": true, "participants": []}')
        with self.assertRaises(ParticipantPinError):
            decode_participants('{"version": 1.0, "participants": []}')


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("ws1", "act:gen7")

    def _plan(self, **kw):
        return self.store.plan_transaction(
            self.key,
            action_generation=kw.get("action_generation", GEN),
            decision=kw.get("decision", _decision()),
            continuation=kw.get("continuation", _continuation()),
            participants=kw.get(
                "participants", [_gateway(), _worker(), _self_coordinator()]
            ),
        )

    def _rev(self):
        return self.store.get(self.key).revision

    def _claim(self, holder="fresh-cx", gen=GEN, rev=None):
        if rev is None:
            rev = self._rev()
        return self.store.claim(
            self.key, expected_revision=rev, expected_action_generation=gen,
            holder=holder, lease_expires_at=FUTURE,
        )

    def _phase(self, target, holder="H", gen=GEN, now=None):
        return self.store.transition_phase(
            self.key, expected_revision=self._rev(),
            expected_action_generation=gen, target=target, holder=holder, now=now,
        )

    def _participant(self, identity, target, holder="H", gen=GEN):
        return self.store.transition_participant(
            self.key, expected_revision=self._rev(),
            expected_action_generation=gen, identity=identity, target=target,
            holder=holder,
        )

    def _replace_participant(self, identity, holder="H"):
        for tgt in (
            PARTICIPANT_LAUNCH_OWED, PARTICIPANT_VERIFY_OWED, PARTICIPANT_REPLACED,
        ):
            out = self._participant(identity, tgt, holder=holder)
            self.assertTrue(out.applied, f"{identity} -> {tgt}: {out}")


class PlanTests(_StoreCase):
    def test_plan_creates_immutable_header_at_planned(self):
        out = self._plan()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        self.assertEqual(out.revision, 1)
        rec = self.store.get(self.key)
        self.assertEqual(rec.phase, PHASE_PLANNED)
        self.assertEqual(rec.action_generation, GEN)
        self.assertEqual(len(rec.participants), 3)
        self.assertEqual(rec.decision, _decision())
        self.assertEqual(rec.continuation, _continuation())
        self.assertEqual(rec.lease_holder, "")
        self.assertEqual(rec.lease_epoch, 0)
        for pin in rec.participants:
            self.assertEqual(pin.phase, PARTICIPANT_CLOSE_OWED)

    def test_exact_replan_of_pristine_row_is_idempotent(self):
        self._plan()
        out = self._plan()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        self.assertEqual(out.revision, 1)

    def test_divergent_replan_is_already_declared_zero_write(self):
        self._plan()
        out = self._plan(action_generation=8)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self.store.get(self.key).action_generation, GEN)

    def test_claim_only_advanced_replan_is_conflict(self):
        # R1-F5: a claimed (but participant-unmoved) row is NOT a pristine re-plan.
        self._plan()
        self._claim()  # revision 2, lease held; participants still all close_owed
        out = self._plan()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)

    def test_participant_advanced_replan_is_conflict(self):
        self._plan()
        self._claim()
        self._phase(PHASE_CLAIMED, holder="fresh-cx")
        self._phase(PHASE_REPLACING_NONSELF, holder="fresh-cx")
        self._participant(_gateway().identity, PARTICIPANT_LAUNCH_OWED, holder="fresh-cx")
        out = self._plan()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)

    def test_plan_rejects_nonpositive_and_bool_generation(self):
        with self.assertRaises(ValueError):
            self._plan(action_generation=0)
        with self.assertRaises(ValueError):
            self._plan(action_generation=True)


class LeaseTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self._plan()

    def test_claim_acquire_bumps_epoch_and_revision(self):
        out = self._claim()
        self.assertTrue(out.applied)
        self.assertEqual(out.revision, 2)
        rec = self.store.get(self.key)
        self.assertEqual(rec.lease_holder, "fresh-cx")
        self.assertEqual(rec.lease_epoch, 1)

    def test_live_lease_is_not_stealable(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        out = self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="B", lease_expires_at=FUTURE,
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_CONFLICT)

    def test_same_holder_reclaims_on_resume(self):
        self._claim(holder="A")
        out = self._claim(holder="A")
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).lease_epoch, 2)

    def test_expired_lease_is_reclaimable_by_new_holder(self):
        self.store.claim(
            self.key, expected_revision=1, expected_action_generation=GEN, holder="A",
            lease_expires_at="2020-01-01T00:00:00+00:00",
            now="2019-01-01T00:00:00+00:00",
        )
        rec = self.store.get(self.key)
        out = self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="B", lease_expires_at=FUTURE, now="2050-01-01T00:00:00+00:00",
        )
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).lease_holder, "B")

    def test_claim_stale_revision_refused(self):
        self._claim()
        out = self.store.claim(
            self.key, expected_revision=1, expected_action_generation=GEN,
            holder="X", lease_expires_at=FUTURE,
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)

    def test_claim_generation_mismatch_refused(self):
        # R1-F2: a caller on a stale/recycled generation is zero-write.
        out = self.store.claim(
            self.key, expected_revision=1, expected_action_generation=GEN + 1,
            holder="X", lease_expires_at=FUTURE,
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_GENERATION_MISMATCH)

    def test_renew_only_by_live_holder(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        bad = self.store.renew(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="B", lease_expires_at=FUTURE,
        )
        self.assertFalse(bad.applied)
        self.assertEqual(bad.reason, CAS_LEASE_NOT_HELD)
        ok = self.store.renew(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="A", lease_expires_at=FUTURE,
        )
        self.assertTrue(ok.applied)
        self.assertEqual(self.store.get(self.key).lease_epoch, 1)  # epoch unchanged

    def test_expired_holder_cannot_renew(self):
        # R1-F4: an expired holder must re-claim (epoch bump), never renew a lapsed lease.
        self.store.claim(
            self.key, expected_revision=1, expected_action_generation=GEN, holder="A",
            lease_expires_at="2020-01-01T00:00:00+00:00",
            now="2019-01-01T00:00:00+00:00",
        )
        rec = self.store.get(self.key)
        out = self.store.renew(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="A", lease_expires_at="2060-01-01T00:00:00+00:00",
            now="2050-01-01T00:00:00+00:00",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)
        after = self.store.get(self.key)
        self.assertEqual(after.lease_expires_at, "2020-01-01T00:00:00+00:00")  # unchanged

    def test_release_only_by_holder_and_clears_lease(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        bad = self.store.release(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="B",
        )
        self.assertFalse(bad.applied)
        self.assertEqual(bad.reason, CAS_LEASE_NOT_HELD)
        ok = self.store.release(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="A",
        )
        self.assertTrue(ok.applied)
        after = self.store.get(self.key)
        self.assertEqual(after.lease_holder, "")
        self.assertEqual(after.lease_expires_at, "")

    def test_claim_missing_row_not_found(self):
        out = self.store.claim(
            ReplacementTransactionKey("ws1", "nope"),
            expected_revision=1, expected_action_generation=GEN, holder="X",
            lease_expires_at=FUTURE,
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)


class PhaseTransitionTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self._plan()
        self._claim(holder="H")

    def test_phase_advances_under_live_holder(self):
        out = self._phase(PHASE_CLAIMED)
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).phase, PHASE_CLAIMED)

    def test_phase_requires_lease_holder(self):
        out = self._phase(PHASE_CLAIMED, holder="stranger")
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_phase_requires_live_lease(self):
        rec = self.store.get(self.key)
        self.store.claim(
            self.key, expected_revision=rec.revision, expected_action_generation=GEN,
            holder="H", lease_expires_at="2020-01-01T00:00:00+00:00",
            now="2019-01-01T00:00:00+00:00",
        )
        out = self._phase(PHASE_CLAIMED, holder="H", now="2050-01-01T00:00:00+00:00")
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_phase_generation_mismatch_refused(self):
        out = self._phase(PHASE_CLAIMED, gen=GEN + 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_GENERATION_MISMATCH)

    def test_illegal_edge_refused(self):
        out = self._phase(PHASE_REPLACING_NONSELF)  # planned -> replacing_nonself skips claimed
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_cannot_leave_replacing_nonself_until_nonself_replaced(self):
        # R1-F1: awaiting_self_turn_end requires all non-self participants replaced.
        self._phase(PHASE_CLAIMED)
        self._phase(PHASE_REPLACING_NONSELF)
        out = self._phase(PHASE_AWAITING_SELF_TURN_END)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        # replace only ONE of the two non-self -> still blocked
        self._replace_participant(_gateway().identity)
        out = self._phase(PHASE_AWAITING_SELF_TURN_END)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        # replace the other -> now allowed
        self._replace_participant(_worker().identity)
        self.assertTrue(self._phase(PHASE_AWAITING_SELF_TURN_END).applied)

    def test_cannot_complete_with_unreplaced_self(self):
        # Walk to draining_continuation with self still un-replaced, then completed blocks.
        self._phase(PHASE_CLAIMED)
        self._phase(PHASE_REPLACING_NONSELF)
        self._replace_participant(_gateway().identity)
        self._replace_participant(_worker().identity)
        self._phase(PHASE_AWAITING_SELF_TURN_END)
        self._phase(PHASE_SELF_CLOSE_ARMED)
        self._phase(PHASE_FRESH_COORDINATOR_CLAIMED)
        self._phase(PHASE_DRAINING_CONTINUATION)
        out = self._phase(PHASE_COMPLETED)  # self still close_owed
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_full_valid_choreography_to_completed(self):
        self._phase(PHASE_CLAIMED)
        self._phase(PHASE_REPLACING_NONSELF)
        self._replace_participant(_gateway().identity)
        self._replace_participant(_worker().identity)
        self._phase(PHASE_AWAITING_SELF_TURN_END)
        self._phase(PHASE_SELF_CLOSE_ARMED)
        self._replace_participant(_self_coordinator().identity)  # self replaced last
        self._phase(PHASE_FRESH_COORDINATOR_CLAIMED)
        self._phase(PHASE_DRAINING_CONTINUATION)
        self.assertTrue(self._phase(PHASE_COMPLETED).applied)
        rec = self.store.get(self.key)
        self.assertEqual(rec.phase, PHASE_COMPLETED)
        self.assertTrue(all(p.phase == PARTICIPANT_REPLACED for p in rec.participants))

    def test_unknown_phase_raises(self):
        with self.assertRaises(ValueError):
            self.store.transition_phase(
                self.key, expected_revision=2, expected_action_generation=GEN,
                target="bogus", holder="H",
            )


class ParticipantTransitionTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self._plan()
        self._claim(holder="H")
        self._phase(PHASE_CLAIMED)
        self._phase(PHASE_REPLACING_NONSELF)  # non-self participants may now be actuated
        self.tid = _gateway().identity

    def test_owed_progression_and_identity_preserved(self):
        for target in (
            PARTICIPANT_LAUNCH_OWED, PARTICIPANT_VERIFY_OWED, PARTICIPANT_REPLACED,
        ):
            out = self._participant(self.tid, target)
            self.assertTrue(out.applied, f"{target}: {out}")
        rec = self.store.get(self.key)
        gw = rec.find_participant(self.tid)
        self.assertEqual(gw.phase, PARTICIPANT_REPLACED)
        self.assertEqual(gw.old_locator, "w28:p1")
        self.assertEqual(gw.identity, self.tid)
        self.assertEqual(
            rec.find_participant(_worker().identity).phase, PARTICIPANT_CLOSE_OWED
        )

    def test_launch_owed_self_loop_is_replay_safe(self):
        self._participant(self.tid, PARTICIPANT_LAUNCH_OWED)
        out = self._participant(self.tid, PARTICIPANT_LAUNCH_OWED)
        self.assertTrue(out.applied)

    def test_illegal_skip_refused(self):
        out = self._participant(self.tid, PARTICIPANT_REPLACED)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_unknown_participant_not_found(self):
        out = self._participant(("a", "b", "c", "d"), PARTICIPANT_LAUNCH_OWED)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_PARTICIPANT_NOT_FOUND)

    def test_participant_move_requires_lease_holder(self):
        out = self._participant(self.tid, PARTICIPANT_LAUNCH_OWED, holder="stranger")
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_participant_generation_mismatch_refused(self):
        out = self._participant(self.tid, PARTICIPANT_LAUNCH_OWED, gen=GEN + 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_GENERATION_MISMATCH)

    def test_self_participant_cannot_move_before_self_close_armed(self):
        # R1-F1: the self coordinator is replaced last; it cannot move in replacing_nonself.
        out = self._participant(_self_coordinator().identity, PARTICIPANT_LAUNCH_OWED)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_nonself_participant_cannot_move_before_replacing_nonself(self):
        # R1-F1: a non-self participant may be actuated only while in `replacing_nonself`.
        # Build a fresh transaction stopped at `claimed` to isolate the actuation gate from
        # the participant edge (gateway is still at close_owed, a legal owed edge).
        key = ReplacementTransactionKey("ws1", "act:other")
        self.store.plan_transaction(
            key, action_generation=GEN, decision=_decision(),
            continuation=_continuation(),
            participants=[_gateway(), _worker(), _self_coordinator()],
        )
        self.store.claim(
            key, expected_revision=1, expected_action_generation=GEN, holder="H",
            lease_expires_at=FUTURE,
        )
        rev = self.store.get(key).revision
        self.store.transition_phase(
            key, expected_revision=rev, expected_action_generation=GEN,
            target=PHASE_CLAIMED, holder="H",
        )
        rev = self.store.get(key).revision
        out = self.store.transition_participant(  # phase is `claimed`, not replacing_nonself
            key, expected_revision=rev, expected_action_generation=GEN,
            identity=_gateway().identity, target=PARTICIPANT_LAUNCH_OWED, holder="H",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_stale_revision_refused(self):
        first = self._rev()
        self._participant(self.tid, PARTICIPANT_LAUNCH_OWED)
        out = self.store.transition_participant(
            self.key, expected_revision=first, expected_action_generation=GEN,
            identity=self.tid, target=PARTICIPANT_VERIFY_OWED, holder="H",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)


class SchemaRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = replacement_transaction_path(self.home)

    def _components(self):
        conn = sqlite3.connect(self.path)
        try:
            return {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT component, schema_version, recovery_policy, migrated_from "
                    "FROM state_schema_components"
                )
            }
        finally:
            conn.close()

    def test_registers_native_component_v1(self):
        ensure_replacement_transaction_schema(self.path)
        comps = self._components()
        self.assertIn(REPLACEMENT_TRANSACTION_COMPONENT, comps)
        version, policy, _migrated = comps[REPLACEMENT_TRANSACTION_COMPONENT]
        self.assertEqual(version, REPLACEMENT_TRANSACTION_SCHEMA_VERSION)
        self.assertEqual(policy, REPLACEMENT_TRANSACTION_RECOVERY_POLICY)

    def test_native_registration_has_no_migrated_from(self):
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT migrated_from FROM state_schema_components WHERE component = ?",
                (REPLACEMENT_TRANSACTION_COMPONENT,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row[0])

    def test_coexists_with_lifecycle_component_on_one_container(self):
        from mozyo_bridge.core.state.lane_lifecycle import (
            LANE_LIFECYCLE_COMPONENT,
            LaneLifecycleStore,
        )

        LaneLifecycleStore(home=self.home).ensure_schema()
        ensure_replacement_transaction_schema(self.path)
        comps = self._components()
        self.assertIn(LANE_LIFECYCLE_COMPONENT, comps)
        self.assertIn(REPLACEMENT_TRANSACTION_COMPONENT, comps)

    def test_idempotent_ensure(self):
        ensure_replacement_transaction_schema(self.path)
        ensure_replacement_transaction_schema(self.path)
        self.assertEqual(
            self._components()[REPLACEMENT_TRANSACTION_COMPONENT][0],
            REPLACEMENT_TRANSACTION_SCHEMA_VERSION,
        )


class SchemaDowngradeGuardTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = replacement_transaction_path(self.home)
        ensure_replacement_transaction_schema(self.path)

    def _set_recorded_version(self, value):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (value, REPLACEMENT_TRANSACTION_COMPONENT),
            )
            conn.commit()
        finally:
            conn.close()

    def test_newer_version_fails_closed_untouched(self):
        self._set_recorded_version(REPLACEMENT_TRANSACTION_SCHEMA_VERSION + 1)
        with self.assertRaises(ReplacementTransactionError):
            ensure_replacement_transaction_schema(self.path)

    def test_malformed_version_fails_closed(self):
        self._set_recorded_version(2.5)
        with self.assertRaises(ReplacementTransactionError):
            ensure_replacement_transaction_schema(self.path)

    def test_table_without_metadata_fails_closed(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "DELETE FROM state_schema_components WHERE component = ?",
                (REPLACEMENT_TRANSACTION_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ReplacementTransactionError):
            ensure_replacement_transaction_schema(self.path)

    def test_extra_column_is_not_a_known_shape(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                f"ALTER TABLE {REPLACEMENT_TABLE} ADD COLUMN bogus TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ReplacementTransactionError):
            ensure_replacement_transaction_schema(self.path)


class ReadonlyStatusTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = replacement_transaction_path(self.home)

    def test_absent_when_no_state_file(self):
        self.assertEqual(load_replacement_transactions_readonly(home=self.home), ())
        self.assertFalse(self.path.exists())

    def test_recognized_after_ensure(self):
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                readonly_component_status(conn), READONLY_COMPONENT_RECOGNIZED
            )
        finally:
            conn.close()

    def test_absent_status_on_bare_container(self):
        from mozyo_bridge.core.state.state_store import connect_state_container_rw

        connect_state_container_rw(self.path).close()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                readonly_component_status(conn), READONLY_COMPONENT_ABSENT
            )
        finally:
            conn.close()

    def test_unsupported_status_on_newer_version(self):
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (REPLACEMENT_TRANSACTION_SCHEMA_VERSION + 1,
                 REPLACEMENT_TRANSACTION_COMPONENT),
            )
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                readonly_component_status(conn), READONLY_COMPONENT_UNSUPPORTED
            )
        finally:
            conn.close()

    def test_readonly_exact_shape_mismatch_is_unsupported(self):
        # R1-F3: the read path must reject a foreign/partial shape the write path rejects.
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                f"ALTER TABLE {REPLACEMENT_TABLE} ADD COLUMN bogus TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                readonly_component_status(conn), READONLY_COMPONENT_UNSUPPORTED
            )
        finally:
            conn.close()
        # and the read-only loader fails closed (None), never reads authority rows.
        self.assertIsNone(load_replacement_transactions_readonly(home=self.home))

    def test_readonly_load_fails_closed_on_unsupported(self):
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (REPLACEMENT_TRANSACTION_SCHEMA_VERSION + 1,
                 REPLACEMENT_TRANSACTION_COMPONENT),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(load_replacement_transactions_readonly(home=self.home))


class ModuleLevelReadTests(_StoreCase):
    def test_load_returns_rows(self):
        self._plan()
        rows = load_replacement_transactions(home=self.home)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action_id, "act:gen7")

    def test_isolated_home_never_touches_default(self):
        self._plan()
        self.assertTrue(
            str(replacement_transaction_path(self.home)).startswith(str(self.home))
        )


class PreservationPlannerTests(unittest.TestCase):
    def test_all_clear_may_close(self):
        v = assess_preservation(
            PreservationObservation(identity_matches=True, attestation_fresh=True)
        )
        self.assertTrue(v.may_close)
        self.assertEqual(v.reasons, ())

    def test_each_fence_blocks(self):
        base = dict(identity_matches=True, attestation_fresh=True)
        self.assertEqual(
            assess_preservation(PreservationObservation(dirty_diff=True, **base)).reasons,
            (PRESERVE_DIRTY_DIFF,),
        )
        self.assertEqual(
            assess_preservation(
                PreservationObservation(running_process=True, **base)
            ).reasons,
            (PRESERVE_RUNNING_PROCESS,),
        )
        self.assertEqual(
            assess_preservation(
                PreservationObservation(unrecorded_journal=True, **base)
            ).reasons,
            (PRESERVE_UNRECORDED_JOURNAL,),
        )
        self.assertEqual(
            assess_preservation(
                PreservationObservation(pending_approval=True, **base)
            ).reasons,
            (PRESERVE_PENDING_APPROVAL,),
        )
        self.assertEqual(
            assess_preservation(
                PreservationObservation(identity_matches=False, attestation_fresh=True)
            ).reasons,
            (PRESERVE_IDENTITY_MISMATCH,),
        )
        self.assertEqual(
            assess_preservation(
                PreservationObservation(identity_matches=True, attestation_fresh=False)
            ).reasons,
            (PRESERVE_ATTESTATION_MISSING,),
        )

    def test_default_observation_fails_closed(self):
        v = assess_preservation(PreservationObservation())
        self.assertTrue(v.blocked)
        self.assertIn(PRESERVE_IDENTITY_MISMATCH, v.reasons)
        self.assertIn(PRESERVE_ATTESTATION_MISSING, v.reasons)

    def test_reasons_are_additive_and_ordered(self):
        v = assess_preservation(
            PreservationObservation(
                pending_approval=True, dirty_diff=True,
                identity_matches=True, attestation_fresh=True,
            )
        )
        self.assertEqual(v.reasons, (PRESERVE_DIRTY_DIFF, PRESERVE_PENDING_APPROVAL))
        self.assertEqual(
            v.reasons, tuple(r for r in PRESERVATION_REASONS if r in v.reasons)
        )

    def test_identity_helper_matches_full_pin_including_lifecycle(self):
        wk = _worker()
        self.assertTrue(
            identity_observation_for(
                wk, observed_lane_id="lane_a", observed_role="worker",
                observed_provider="claude", observed_assigned_name="wk1",
                observed_locator="w28:p2", observed_lane_revision="5",
                observed_lane_generation="2",
            )
        )
        self.assertFalse(
            identity_observation_for(
                wk, observed_lane_id="lane_a", observed_role="worker",
                observed_provider="claude", observed_assigned_name="wk1",
                observed_locator="w28:p2", observed_lane_revision="6",
                observed_lane_generation="2",
            )
        )
        self.assertFalse(
            identity_observation_for(
                wk, observed_lane_id="lane_a", observed_role="worker",
                observed_provider="claude", observed_assigned_name="wk1",
                observed_locator="w99:p9", observed_lane_revision="5",
                observed_lane_generation="2",
            )
        )

    def test_identity_helper_ignores_lifecycle_when_pin_unbound(self):
        gw = _gateway()
        self.assertTrue(
            identity_observation_for(
                gw, observed_lane_id="lane_a", observed_role="gateway",
                observed_provider="codex", observed_assigned_name="gw1",
                observed_locator="w28:p1",
            )
        )

    def test_verdict_payload_shape(self):
        v = assess_preservation(PreservationObservation(dirty_diff=True))
        payload = v.as_payload()
        self.assertFalse(payload["may_close"])
        self.assertIn(PRESERVE_DIRTY_DIFF, payload["reasons"])


if __name__ == "__main__":
    unittest.main()
