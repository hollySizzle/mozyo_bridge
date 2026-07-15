"""Redmine #13806 tranche A — atomic self-replacement transaction core.

The "1 action generation = 1 durable replacement transaction" substrate (Design Answer
j#78384, Coordinator Verdict j#78406, Implementation Request j#78948): the session /
workspace-scoped replacement transaction component that binds several participants + a
post-self-close continuation into one owner-approved generation, WITHOUT pushing the
default coordinator into an issue lane's #13810 lifecycle row. Pins:

- the pure model: the transaction phase DAG, the participant owed progression, the
  continuation-pointer / participant-manifest codecs and their fail-closed decoders;
- the store: plan (immutable header, idempotent exact re-plan), the lease
  (claim/renew/release with conflict / expiry / epoch), the phase + participant CAS with
  lease-ownership and exact-revision guards, and partial-replay self-loops;
- the schema: native-component registration on the shared ``state.sqlite``, coexistence
  with the lifecycle component, the exact-shape classifier, and the downgrade fail-closed
  guard;
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
    participant_transition_allowed,
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
    PreservationVerdict,
    assess_preservation,
    identity_observation_for,
)

FUTURE = "2099-01-01T00:00:00+00:00"


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
        # No skips, no self-loops, terminal is terminal.
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
        # close_owed does not self-loop; you cannot skip; replaced is terminal.
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


class ContinuationPointerTests(unittest.TestCase):
    def test_requires_readable_anchor_and_closed_tokens(self):
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="0", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            )  # zero id
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="asana", issue_id="1", journal_id="1",
                expected_gate="g", next_semantic_action="n",
            )  # unknown source
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="1", journal_id="1",
                expected_gate="", next_semantic_action="n",
            )  # empty gate
        with self.assertRaises(ContinuationPointerError):
            ContinuationPointer(
                source="redmine", issue_id="1", journal_id="1",
                expected_gate="g", next_semantic_action="",
            )  # empty action

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
        # Stable across a re-encode of the decoded set (sorted by identity).
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
            # newer version -> fail closed, never drop fields
            decode_participants(
                '{"version": %d, "participants": []}' % (PARTICIPANTS_VERSION + 1)
            )
        with self.assertRaises(ParticipantPinError):
            # bool folds to 1 but is not an int version
            decode_participants('{"version": true, "participants": []}')
        with self.assertRaises(ParticipantPinError):
            # float folds to 1 but is not an exact integer version
            decode_participants('{"version": 1.0, "participants": []}')


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("ws1", "act:gen7")

    def _plan(self, **kw):
        return self.store.plan_transaction(
            self.key,
            action_generation=kw.get("action_generation", 7),
            decision=kw.get("decision", _decision()),
            continuation=kw.get("continuation", _continuation()),
            participants=kw.get(
                "participants", [_gateway(), _worker(), _self_coordinator()]
            ),
        )

    def _claim(self, holder="fresh-cx", rev=None):
        if rev is None:
            rev = self.store.get(self.key).revision
        return self.store.claim(
            self.key, expected_revision=rev, holder=holder, lease_expires_at=FUTURE
        )


class PlanTests(_StoreCase):
    def test_plan_creates_immutable_header_at_planned(self):
        out = self._plan()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        self.assertEqual(out.revision, 1)
        rec = self.store.get(self.key)
        self.assertEqual(rec.phase, PHASE_PLANNED)
        self.assertEqual(rec.action_generation, 7)
        self.assertEqual(len(rec.participants), 3)
        self.assertEqual(rec.decision, _decision())
        self.assertEqual(rec.continuation, _continuation())
        self.assertEqual(rec.lease_holder, "")
        self.assertEqual(rec.lease_epoch, 0)
        for pin in rec.participants:
            self.assertEqual(pin.phase, PARTICIPANT_CLOSE_OWED)

    def test_exact_replan_is_idempotent(self):
        self._plan()
        out = self._plan()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        self.assertEqual(out.revision, 1)  # not bumped

    def test_divergent_replan_is_already_declared_zero_write(self):
        self._plan()
        out = self._plan(action_generation=8)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self.store.get(self.key).action_generation, 7)  # unchanged

    def test_replan_after_participant_moved_is_conflict(self):
        # An in-flight transaction's manifest has advanced phases, so a fresh
        # planned manifest no longer matches -> a re-plan is refused (never revives).
        self._plan()
        self._claim()
        rec = self.store.get(self.key)
        self.store.transition_phase(
            self.key, expected_revision=rec.revision, target=PHASE_CLAIMED,
            holder="fresh-cx",
        )
        rec = self.store.get(self.key)
        self.store.transition_participant(
            self.key, expected_revision=rec.revision, identity=_gateway().identity,
            target=PARTICIPANT_LAUNCH_OWED, holder="fresh-cx",
        )
        out = self._plan()  # identical header, but manifest phases advanced
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
            self.key, expected_revision=rec.revision, holder="B", lease_expires_at=FUTURE
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_CONFLICT)

    def test_same_holder_reclaims_on_resume(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        out = self.store.claim(
            self.key, expected_revision=rec.revision, holder="A", lease_expires_at=FUTURE
        )
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).lease_epoch, 2)

    def test_expired_lease_is_reclaimable_by_new_holder(self):
        self.store.claim(
            self.key, expected_revision=1, holder="A",
            lease_expires_at="2020-01-01T00:00:00+00:00",
            now="2019-01-01T00:00:00+00:00",
        )
        rec = self.store.get(self.key)
        out = self.store.claim(
            self.key, expected_revision=rec.revision, holder="B",
            lease_expires_at=FUTURE, now="2050-01-01T00:00:00+00:00",
        )
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).lease_holder, "B")

    def test_claim_stale_revision_refused(self):
        self._claim()
        out = self.store.claim(
            self.key, expected_revision=1, holder="X", lease_expires_at=FUTURE
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)

    def test_renew_only_by_holder(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        bad = self.store.renew(
            self.key, expected_revision=rec.revision, holder="B", lease_expires_at=FUTURE
        )
        self.assertFalse(bad.applied)
        self.assertEqual(bad.reason, CAS_LEASE_NOT_HELD)
        ok = self.store.renew(
            self.key, expected_revision=rec.revision, holder="A", lease_expires_at=FUTURE
        )
        self.assertTrue(ok.applied)
        self.assertEqual(self.store.get(self.key).lease_epoch, 1)  # epoch unchanged

    def test_release_only_by_holder_and_clears_lease(self):
        self._claim(holder="A")
        rec = self.store.get(self.key)
        bad = self.store.release(self.key, expected_revision=rec.revision, holder="B")
        self.assertFalse(bad.applied)
        self.assertEqual(bad.reason, CAS_LEASE_NOT_HELD)
        ok = self.store.release(self.key, expected_revision=rec.revision, holder="A")
        self.assertTrue(ok.applied)
        after = self.store.get(self.key)
        self.assertEqual(after.lease_holder, "")
        self.assertEqual(after.lease_expires_at, "")

    def test_claim_missing_row_not_found(self):
        out = self.store.claim(
            ReplacementTransactionKey("ws1", "nope"),
            expected_revision=1, holder="X", lease_expires_at=FUTURE,
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)


class PhaseTransitionTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self._plan()
        self._claim(holder="H")

    def test_phase_advances_under_live_holder(self):
        rec = self.store.get(self.key)
        out = self.store.transition_phase(
            self.key, expected_revision=rec.revision, target=PHASE_CLAIMED, holder="H"
        )
        self.assertTrue(out.applied)
        self.assertEqual(self.store.get(self.key).phase, PHASE_CLAIMED)

    def test_phase_requires_lease_holder(self):
        rec = self.store.get(self.key)
        out = self.store.transition_phase(
            self.key, expected_revision=rec.revision, target=PHASE_CLAIMED,
            holder="stranger",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_phase_requires_live_lease(self):
        # Re-claim with an expiry in the past relative to the transition's `now`.
        rec = self.store.get(self.key)
        self.store.claim(
            self.key, expected_revision=rec.revision, holder="H",
            lease_expires_at="2020-01-01T00:00:00+00:00",
            now="2019-01-01T00:00:00+00:00",
        )
        rec = self.store.get(self.key)
        out = self.store.transition_phase(
            self.key, expected_revision=rec.revision, target=PHASE_CLAIMED,
            holder="H", now="2050-01-01T00:00:00+00:00",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_illegal_edge_refused(self):
        rec = self.store.get(self.key)
        out = self.store.transition_phase(
            self.key, expected_revision=rec.revision,
            target=PHASE_REPLACING_NONSELF, holder="H",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_full_linear_walk_to_completed(self):
        order = [
            PHASE_CLAIMED,
            PHASE_REPLACING_NONSELF,
            PHASE_AWAITING_SELF_TURN_END,
            PHASE_SELF_CLOSE_ARMED,
            PHASE_FRESH_COORDINATOR_CLAIMED,
            PHASE_DRAINING_CONTINUATION,
            PHASE_COMPLETED,
        ]
        for target in order:
            rec = self.store.get(self.key)
            # Keep the lease live across the walk (renew is not needed; FUTURE holds).
            out = self.store.transition_phase(
                self.key, expected_revision=rec.revision, target=target, holder="H"
            )
            self.assertTrue(out.applied, f"{target}: {out}")
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)

    def test_unknown_phase_raises(self):
        with self.assertRaises(ValueError):
            self.store.transition_phase(
                self.key, expected_revision=2, target="bogus", holder="H"
            )


class ParticipantTransitionTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self._plan()
        self._claim(holder="H")
        self.tid = _gateway().identity

    def _rev(self):
        return self.store.get(self.key).revision

    def test_owed_progression_and_identity_preserved(self):
        for target in (
            PARTICIPANT_LAUNCH_OWED,
            PARTICIPANT_VERIFY_OWED,
            PARTICIPANT_REPLACED,
        ):
            out = self.store.transition_participant(
                self.key, expected_revision=self._rev(), identity=self.tid,
                target=target, holder="H",
            )
            self.assertTrue(out.applied, f"{target}: {out}")
        rec = self.store.get(self.key)
        gw = rec.find_participant(self.tid)
        self.assertEqual(gw.phase, PARTICIPANT_REPLACED)
        # Identity + evidence untouched by the phase moves.
        self.assertEqual(gw.old_locator, "w28:p1")
        self.assertEqual(gw.identity, self.tid)
        # The other participants are untouched.
        self.assertEqual(
            rec.find_participant(_worker().identity).phase, PARTICIPANT_CLOSE_OWED
        )

    def test_launch_owed_self_loop_is_replay_safe(self):
        self.store.transition_participant(
            self.key, expected_revision=self._rev(), identity=self.tid,
            target=PARTICIPANT_LAUNCH_OWED, holder="H",
        )
        out = self.store.transition_participant(
            self.key, expected_revision=self._rev(), identity=self.tid,
            target=PARTICIPANT_LAUNCH_OWED, holder="H",
        )
        self.assertTrue(out.applied)

    def test_illegal_skip_refused(self):
        out = self.store.transition_participant(
            self.key, expected_revision=self._rev(), identity=self.tid,
            target=PARTICIPANT_REPLACED, holder="H",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)

    def test_unknown_participant_not_found(self):
        out = self.store.transition_participant(
            self.key, expected_revision=self._rev(), identity=("a", "b", "c", "d"),
            target=PARTICIPANT_LAUNCH_OWED, holder="H",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_PARTICIPANT_NOT_FOUND)

    def test_participant_move_requires_lease_holder(self):
        out = self.store.transition_participant(
            self.key, expected_revision=self._rev(), identity=self.tid,
            target=PARTICIPANT_LAUNCH_OWED, holder="stranger",
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_LEASE_NOT_HELD)

    def test_stale_revision_refused(self):
        first = self._rev()
        self.store.transition_participant(
            self.key, expected_revision=first, identity=self.tid,
            target=PARTICIPANT_LAUNCH_OWED, holder="H",
        )
        out = self.store.transition_participant(
            self.key, expected_revision=first, identity=self.tid,
            target=PARTICIPANT_VERIFY_OWED, holder="H",
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
        # The lifecycle store and this store share the one state.sqlite container.
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
        ensure_replacement_transaction_schema(self.path)  # no raise, no duplicate
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
        self._set_recorded_version(2.5)  # a REAL, not an exact integer
        with self.assertRaises(ReplacementTransactionError):
            ensure_replacement_transaction_schema(self.path)

    def test_table_without_metadata_fails_closed(self):
        # Drop the component metadata row but leave the table -> partial/unknown state.
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
        # A non-creating read of an absent store yields () (nothing created).
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
        # A container with the components registry but no replacement component/table.
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
        # The store path is strictly under the isolated home.
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
        # deterministic PRESERVATION_REASONS order
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
        # a diverged lifecycle revision fails the fence
        self.assertFalse(
            identity_observation_for(
                wk, observed_lane_id="lane_a", observed_role="worker",
                observed_provider="claude", observed_assigned_name="wk1",
                observed_locator="w28:p2", observed_lane_revision="6",
                observed_lane_generation="2",
            )
        )
        # a diverged locator fails the fence
        self.assertFalse(
            identity_observation_for(
                wk, observed_lane_id="lane_a", observed_role="worker",
                observed_provider="claude", observed_assigned_name="wk1",
                observed_locator="w99:p9", observed_lane_revision="5",
                observed_lane_generation="2",
            )
        )

    def test_identity_helper_ignores_lifecycle_when_pin_unbound(self):
        # The self coordinator carries no lifecycle pin, so it does not constrain those.
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
