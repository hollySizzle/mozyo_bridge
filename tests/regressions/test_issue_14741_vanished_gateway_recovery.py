"""A vanished-gateway recovery's identity and durable plan (#14741 j#97147).

No live effect anywhere in this file: no launch, close, send or append. What it pins is
(1) what makes two recoveries the same action, and (2) what the recovery is allowed to
write once it knows.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    GenerationKey,
    LaunchIdentityReceiptStore,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (  # noqa: E402
    TABLE as REPLACEMENT_TRANSACTION_TABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E402,E501
    RECOVERY_ACTION_GENERATION,
    REFUSE_ACTION_ID_INVALID,
    REFUSE_EVIDENCE_DIVERGENT,
    REFUSE_HOME_INVALID,
    REFUSE_TRANSACTION_UNAVAILABLE,
    plan_fresh_recovery,
    replay_explicit_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E402,E501
    IDENTITY_SCHEMA,
    OUTCOME_LEGACY_DIRECT,
    OUTCOME_RECEIPT_PLANNED,
    OUTCOME_REPLAYED,
    REDISPATCH_GATEWAY_ONCE,
    REFUSE_EVIDENCE_UNAVAILABLE,
    REFUSE_FOREIGN_TRANSACTION,
    REFUSE_GENERATION_MISMATCH,
    REFUSE_GENERATION_UNAVAILABLE,
    REFUSE_UNKNOWN_ACTION_SHAPE,
    ParticipantAuthority,
    RequestAnchor,
    VanishedGatewayRecoveryError,
    recovery_action_id,
    recovery_identity_payload,
)
from tests.support.current_launch_authority import (  # noqa: E402
    LEGACY_ACTION_ID,
    RECEIPT_CAPABLE_ACTION_ID,
    seed_current_generation,
)

WORKSPACE = "ws"
LANE = "issue_14741"
PROVIDER = "codex"
ASSIGNED = "mzb1_ws_codex_gateway"
LOCATOR = "ws:p1"
#: The lane's DECLARED lifecycle pair, as the real store records it: a freshly declared
#: lane is generation 1 at revision 1, and the planner's evidence join is byte-exact on that
#: pair -- so the participant, the receipt and the lifecycle row all have to agree.
REVISION = "1"
GENERATION = "1"
CAUSE = "update_relaunch"
DIGEST = "sha256:" + "c" * 64


def _anchor(**kw) -> RequestAnchor:
    base = dict(source="redmine", issue_id="14741", journal_id="97147")
    base.update(kw)
    return RequestAnchor(**base)


def _authority(**kw) -> ParticipantAuthority:
    base = dict(
        workspace_id=WORKSPACE,
        lane_id=LANE,
        provider=PROVIDER,
        assigned_name=ASSIGNED,
        old_locator=LOCATOR,
        lane_revision=REVISION,
        lane_generation=GENERATION,
    )
    base.update(kw)
    return ParticipantAuthority(**base)


def _evidenced(**kw) -> ParticipantAuthority:
    return _authority(
        evidence_workspace_id=WORKSPACE,
        evidence_startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
        evidence_cause=CAUSE,
        **kw,
    )


class IdentityTest(unittest.TestCase):
    """What makes two recoveries the SAME durable action."""

    def test_the_payload_is_canonical_and_schema_tagged(self) -> None:
        payload = recovery_identity_payload(_anchor(), _authority())
        self.assertEqual(payload, json.dumps(json.loads(payload), sort_keys=True,
                                             separators=(",", ":")))
        self.assertEqual(json.loads(payload)["schema"], IDENTITY_SCHEMA)

    def test_the_same_request_and_participant_give_the_same_id(self) -> None:
        self.assertEqual(
            recovery_action_id(_anchor(), _authority()),
            recovery_action_id(_anchor(), _authority()),
        )

    def test_every_authority_axis_changes_the_id(self) -> None:
        """If an axis did not move the id, two different participants would share a row."""
        baseline = recovery_action_id(_anchor(), _authority())
        variants = {
            "workspace_id": "OTHER_WS",
            "lane_id": "issue_other",
            "provider": "claude",
            "assigned_name": "someone_else",
            "old_locator": "ws:p9",
            "lane_revision": "8",
            "lane_generation": "lane-gen-2",
        }
        for axis, value in variants.items():
            with self.subTest(axis=axis):
                self.assertNotEqual(
                    recovery_action_id(_anchor(), _authority(**{axis: value})), baseline
                )
        for axis, value in (
            ("evidence_workspace_id", "OTHER_WS"),
            ("evidence_startup_action_id", "startup-ir1-" + "d" * 64),
            ("evidence_cause", "generic_fresh"),
        ):
            with self.subTest(axis=axis):
                changed = dict(
                    evidence_workspace_id=WORKSPACE,
                    evidence_startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
                    evidence_cause=CAUSE,
                )
                changed[axis] = value
                self.assertNotEqual(
                    recovery_action_id(_anchor(), _authority(**changed)), baseline
                )

    def test_every_anchor_axis_changes_the_id(self) -> None:
        baseline = recovery_action_id(_anchor(), _authority())
        for axis, value in (("issue_id", "99999"), ("journal_id", "11111")):
            with self.subTest(axis=axis):
                self.assertNotEqual(
                    recovery_action_id(_anchor(**{axis: value}), _authority()), baseline
                )

    def test_the_anchor_is_pinned_to_this_redmine_governed_rail(self) -> None:
        """Audit j#97151 R4: pinned in the constructor, not left to a downstream pointer.

        An anchor that only fails when someone builds a pointer out of it has already been
        used to compute an action id by then.
        """
        for kw in (
            dict(source="gitlab"),
            dict(issue_id="00000"),
            dict(journal_id="97147x"),
            dict(issue_id="１４７４１"),  # full-width digits are not ASCII ids
            dict(journal_id="9" * 19),
        ):
            with self.subTest(kw=sorted(kw)):
                with self.assertRaises(VanishedGatewayRecoveryError):
                    _anchor(**kw)

    def test_the_id_is_independent_of_where_and_when_it_is_computed(self) -> None:
        """The retry runs from another checkout, a second later, onto a different pane.

        None of that makes it a different action, so none of it is in the id. Computing it
        from two different working directories must give the same answer.
        """
        import os

        first = recovery_action_id(_anchor(), _authority())
        original = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())
            second = recovery_action_id(_anchor(), _authority())
        finally:
            os.chdir(original)
        self.assertEqual(first, second)

    def test_a_non_gateway_or_self_participant_is_refused(self) -> None:
        with self.assertRaises(VanishedGatewayRecoveryError):
            _authority(role="worker")
        with self.assertRaises(VanishedGatewayRecoveryError):
            _authority(is_self=True)

    def test_a_partial_or_padded_authority_is_refused(self) -> None:
        for kw in (
            dict(lane_id=" issue_14741 "),
            dict(old_locator=""),
            dict(evidence_workspace_id=WORKSPACE),  # partial triplet
            dict(evidence_cause=" " + CAUSE + " ",
                 evidence_workspace_id=WORKSPACE,
                 evidence_startup_action_id=RECEIPT_CAPABLE_ACTION_ID),
        ):
            with self.subTest(kw=sorted(kw)):
                with self.assertRaises(VanishedGatewayRecoveryError):
                    _authority(**kw)

    def test_an_anchor_for_another_gate_is_refused(self) -> None:
        with self.assertRaises(VanishedGatewayRecoveryError):
            _anchor(gate="review_request")


_UNSET = object()


class _PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store_opens = 0
        self.completed_startup = patch(
            "mozyo_bridge.core.state.herdr_launch_generation.completed_generation_startup_token",
            side_effect=lambda _home, generation, **_kw: generation.startup_action_id,
        )
        self.completed_startup.start()
        self.addCleanup(self.completed_startup.stop)

    def _store_factory(self):
        """Lazy by construction: a refusal must not create the transaction database."""
        self.store_opens = getattr(self, "store_opens", 0)

        def factory():
            self.store_opens += 1
            return ReplacementTransactionStore(home=self.home)

        return factory

    def _reader(self):
        """The NON-creating read an explicit replay is allowed: asking must not write."""
        from mozyo_bridge.core.state.replacement_transaction import (
            load_replacement_transactions_readonly,
        )

        self.reader_calls = getattr(self, "reader_calls", 0)

        def read():
            self.reader_calls += 1
            return load_replacement_transactions_readonly(home=self.home)

        return read

    def _plan(self, authority=None, anchor=None, home=_UNSET):
        return plan_fresh_recovery(
            store_factory=self._store_factory(),
            home=self.home if home is _UNSET else home,
            anchor=anchor or _anchor(),
            authority=authority or _authority(),
            live_rows=(),
        )

    def _seed_generation(self, action_id=LEGACY_ACTION_ID, **kw):
        base = dict(
            workspace_id=WORKSPACE, lane_id=LANE, role=PROVIDER,
            assigned_name=ASSIGNED, locator=LOCATOR, action_id=action_id,
        )
        base.update(kw)
        seed_current_generation(self.home, **base)

    def _declare_lifecycle(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
        from mozyo_bridge.core.state.replacement_transaction_model import DecisionPointer

        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(WORKSPACE, LANE),
            decision=DecisionPointer(
                source="redmine", issue_id="14741", journal_id="97147"
            ),
        )

    def _seed_bound_evidence(
        self,
        action_id=RECEIPT_CAPABLE_ACTION_ID,
        locator=LOCATOR,
        lane_generation=GENERATION,
        lifecycle_revision=REVISION,
    ) -> None:
        key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=action_id,
        )
        store = LaunchIdentityReceiptStore(home=self.home)
        store.reserve(key, identity_digest=DIGEST)
        store.finalize(
            key, identity_digest=DIGEST, locator=locator,
            lane_generation=lane_generation, lifecycle_revision=lifecycle_revision,
            composite_proof=True,
        )
        store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )

    def _open_next_lifecycle_generation(self):
        """Advance the lane's declared lifecycle and return its new (generation, revision)."""
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            load_lane_lifecycle_readonly,
        )

        import sqlite3

        with sqlite3.connect(self.home / "state.sqlite") as conn:
            conn.execute(
                "UPDATE lane_lifecycle_records SET lane_generation = lane_generation + 1,"
                " revision = revision + 1 WHERE lane_id = ?",
                (LANE,),
            )
        record = [
            row for row in load_lane_lifecycle_readonly(home=self.home)
            if row.lane_id == LANE
        ][0]
        return (str(record.lane_generation), str(record.revision))

    def _row(self, action_id):
        if not (self.home / "state.sqlite").exists():
            return None
        return ReplacementTransactionStore(home=self.home).get(
            ReplacementTransactionKey(WORKSPACE, action_id)
        )

    def _transaction_db_exists(self) -> bool:
        return (self.home / "state.sqlite").exists()


class LegacyPathTest(_PlanCase):
    def test_an_exact_legacy_action_is_a_direct_heal_with_no_row(self) -> None:
        self._seed_generation()
        plan = self._plan()
        self.assertEqual(plan.decision.outcome, OUTCOME_LEGACY_DIRECT)
        self.assertIsNone(self._row(plan.action_id), "no transaction was written")

    def test_the_legacy_path_never_opens_the_receipt_store(self) -> None:
        """Byte-invariance includes cost: a pre-#14741 heal touches no receipt authority."""
        self._seed_generation()
        opens = []
        import mozyo_bridge.core.state.launch_identity_receipt as receipt

        original = receipt.LaunchIdentityReceiptStore

        class _Counting(original):
            def _connect(self, *, create: bool):
                opens.append(1)
                return super()._connect(create=create)

        receipt.LaunchIdentityReceiptStore = _Counting
        try:
            self.assertEqual(self._plan().decision.outcome, OUTCOME_LEGACY_DIRECT)
        finally:
            receipt.LaunchIdentityReceiptStore = original
        self.assertEqual(opens, [])


class AuthorityRefusalTest(_PlanCase):
    def test_attested_generation_without_completed_startup_is_refused(self):
        self.completed_startup.stop()
        self._seed_generation()
        plan = self._plan()
        self.assertEqual(plan.decision.refusal, REFUSE_GENERATION_MISMATCH)

    def test_the_refusal_matrix_writes_nothing(self) -> None:
        cases = (
            ("no generation row", None, {}, REFUSE_GENERATION_UNAVAILABLE),
            ("pending row", LEGACY_ACTION_ID, {"attested": False}, REFUSE_GENERATION_MISMATCH),
            ("another workspace", LEGACY_ACTION_ID, {"workspace_id": "OTHER"}, REFUSE_GENERATION_MISMATCH),
            ("another lane", LEGACY_ACTION_ID, {"lane_id": "issue_other"}, REFUSE_GENERATION_MISMATCH),
            ("another provider", LEGACY_ACTION_ID, {"role": "claude"}, REFUSE_GENERATION_MISMATCH),
            ("recycled pane", LEGACY_ACTION_ID, {"locator": "ws:p9"}, REFUSE_GENERATION_MISMATCH),
        )
        for label, action_id, overrides, expected in cases:
            with self.subTest(label=label):
                self.setUp()
                if action_id is not None:
                    self._seed_generation(action_id=action_id, **overrides)
                plan = self._plan()
                self.assertEqual(plan.decision.refusal, expected)
                self.assertIsNone(self._row(plan.action_id))

    def test_an_unclassifiable_action_is_never_read_as_legacy(self) -> None:
        self._seed_generation(action_id="startup-" + "z" * 64)
        plan = self._plan()
        self.assertEqual(plan.decision.refusal, REFUSE_UNKNOWN_ACTION_SHAPE)
        self.assertIsNone(self._row(plan.action_id))

    def test_a_capable_action_without_evidence_writes_no_transaction(self) -> None:
        """Absent evidence leaves the recovery unplanned, not planned on an unproven launch."""
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        plan = self._plan(_evidenced())
        self.assertEqual(plan.decision.refusal, REFUSE_EVIDENCE_UNAVAILABLE)
        self.assertIsNone(self._row(plan.action_id))


class ReceiptPlanTest(_PlanCase):
    def _plan_capable(self):
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        return self._plan(_evidenced())

    def test_a_capable_recovery_plans_one_evidenced_participant(self) -> None:
        plan = self._plan_capable()
        self.assertEqual(plan.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        row = self._row(plan.action_id)
        self.assertIsNotNone(row)
        self.assertEqual(len(row.participants), 1)
        pin = row.participants[0]
        self.assertEqual(pin.evidence_startup_action_id, RECEIPT_CAPABLE_ACTION_ID)
        self.assertEqual(pin.evidence_cause, CAUSE)
        self.assertEqual(row.action_generation, RECOVERY_ACTION_GENERATION)
        self.assertEqual(row.continuation.next_semantic_action, REDISPATCH_GATEWAY_ONCE)
        self.assertEqual(row.continuation.expected_gate, "implementation_request")
        self.assertEqual(row.decision.journal_id, "97147")

    def test_the_same_request_replays_the_same_row(self) -> None:
        first = self._plan_capable()
        row_before = self._row(first.action_id)
        again = self._plan(_evidenced())
        self.assertEqual(again.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(again.action_id, first.action_id)
        row_after = self._row(first.action_id)
        self.assertEqual(row_after.revision, row_before.revision, "zero write on replay")
        self.assertEqual(row_after.participants, row_before.participants)

    def _replay(self, action_id, anchor=None, workspace_id=WORKSPACE):
        return replay_explicit_recovery(
            reader=self._reader(),
            workspace_id=workspace_id,
            action_id=action_id,
            anchor=anchor or _anchor(),
        )

    def test_an_explicit_replay_reads_no_world_state_at_all(self) -> None:
        """Audit j#97151 R1: the replay addresses a row, it does not re-derive an id.

        Both authority stores are deleted first, so anything that re-read the current
        generation, the lifecycle or the receipts would refuse instead of resuming.
        """
        first = self._plan_capable()
        (self.home / "herdr-launch-generation.sqlite").unlink()
        (self.home / "launch-identity-receipt.sqlite").unlink()
        again = self._replay(first.action_id)
        self.assertEqual(again.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(again.action_id, first.action_id)

    def test_a_real_g1_to_g2_transition_keeps_g1_reachable_by_its_id(self) -> None:
        """Audit j#97157 R8: the world really moves, in one temp home.

        The first cut only changed the CALLER's authority, so the fresh plan refused on a
        generation mismatch and produced an empty id -- and `assertNotEqual` passed for the
        wrong reason. Here G1 is planned, its evidence consumed, and the generation,
        lifecycle and receipts are advanced to G2 before the fresh plan runs, so the two ids
        are both real.
        """
        g1 = self._plan_capable()
        self.assertEqual(g1.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        g1_row = self._row(g1.action_id)

        # G1's evidence is discharged, exactly as a completed recovery would.
        self.assertEqual(
            LaunchIdentityReceiptStore(home=self.home).consume_evidence(
                GenerationKey(
                    workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
                    assigned_name=ASSIGNED, startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
                ),
                consumed_by=g1.action_id,
            ),
            "consumed",
        )

        # The world moves: a new launch generation at a new pane, a new lifecycle
        # generation, and a fresh bound receipt for the new startup action.
        g2_locator = "ws:p2"
        g2_action = "startup-ir1-" + "e" * 64
        seed_current_generation(
            self.home, workspace_id=WORKSPACE, lane_id=LANE, role=PROVIDER,
            assigned_name=ASSIGNED, locator=g2_locator, action_id=g2_action,
        )
        g2_lifecycle = self._open_next_lifecycle_generation()
        self._seed_bound_evidence(
            action_id=g2_action, locator=g2_locator,
            lane_generation=g2_lifecycle[0], lifecycle_revision=g2_lifecycle[1],
        )

        g2_authority = _authority(
            old_locator=g2_locator,
            lane_generation=g2_lifecycle[0],
            lane_revision=g2_lifecycle[1],
        )
        g2 = self._plan(g2_authority)
        self.assertIn(g2.decision.outcome, (OUTCOME_RECEIPT_PLANNED, OUTCOME_REPLAYED))
        self.assertTrue(g2.action_id, "G2 really planned, so its id is not empty")
        self.assertNotEqual(g2.action_id, g1.action_id)
        self.assertEqual(
            self._row(g2.action_id).participants[0].evidence_startup_action_id, g2_action
        )

        # G1 is still reachable, by its id, unchanged.
        replay = self._replay(g1.action_id)
        self.assertEqual(replay.decision.outcome, OUTCOME_REPLAYED)
        after = self._row(g1.action_id)
        self.assertEqual(after.revision, g1_row.revision, "zero write on the old row")
        self.assertEqual(after.participants, g1_row.participants)

    def test_a_peer_insert_inside_the_plan_call_leaves_the_row_untouched(self) -> None:
        """The actual CAS window (ruling j#97162), stated observationally.

        A peer's exact row lands DURING this run's plan call. Nothing at this seam can say
        who inserted it -- `plan_transaction` answers the same for a pristine re-plan, and
        the deterministic id makes both rows byte-identical -- so the outcome asserted here
        is the observation the contract defines: after its own plan call, this fresh path
        confirmed an exact durable row is ready. What actually matters is that the peer's
        row is not disturbed: same revision, same manifest, same header.
        """
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()

        real = ReplacementTransactionStore(home=self.home)
        peer_rows = []

        class _RacingStore:
            """Empty at the pre-read; the peer lands inside `plan_transaction`."""

            def get(self, key):
                return real.get(key)

            def plan_transaction(self, key, **kwargs):
                if not peer_rows:
                    real.plan_transaction(key, **kwargs)
                    peer_rows.append(real.get(key))
                return real.plan_transaction(key, **kwargs)

        plan = plan_fresh_recovery(
            store_factory=lambda: _RacingStore(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(plan.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        self.assertTrue(peer_rows, "the peer really inserted inside the plan call")
        before = peer_rows[0]
        after = self._row(plan.action_id)
        self.assertEqual(after.revision, before.revision, "the peer's row is untouched")
        self.assertEqual(after.participants_manifest, before.participants_manifest)
        self.assertEqual(after.action_generation, before.action_generation)
        self.assertEqual(after.decision_journal, before.decision_journal)
        self.assertEqual(after.continuation_next_action, before.continuation_next_action)

    def test_an_existing_row_seen_before_the_plan_call_resumes_without_writing(self) -> None:
        """The other observation: an exact row already there is resumed, plan call 0."""
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()

        peer = self._plan(_evidenced())
        self.assertEqual(peer.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        before = self._row(peer.action_id)

        planned = []
        real = ReplacementTransactionStore(home=self.home)

        class _ObservingStore:
            def get(self, key):
                return real.get(key)

            def plan_transaction(self, key, **kwargs):  # pragma: no cover - must not run
                planned.append(key)
                return real.plan_transaction(key, **kwargs)

        again = plan_fresh_recovery(
            store_factory=lambda: _ObservingStore(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(again.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(again.action_id, peer.action_id)
        self.assertEqual(planned, [], "no plan call at all")
        after = self._row(peer.action_id)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.participants, before.participants)

    def test_a_foreign_row_at_the_same_key_is_refused_not_adopted(self) -> None:
        """The other race outcome: someone else's row, same key, zero actuation."""
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        action_id = recovery_action_id(_anchor(), _evidenced())

        class _Foreign:
            def get(self, key):
                return SimpleNamespace(
                    workspace_id=WORKSPACE,
                    action_id=action_id,
                    action_generation=True,  # True == 1 in Python; not an int generation
                    decision_source="redmine",
                    decision_issue_id="14741",
                    decision_journal="97147",
                    continuation_source="redmine",
                    continuation_issue_id="14741",
                    continuation_journal="97147",
                    continuation_expected_gate="implementation_request",
                    continuation_next_action=REDISPATCH_GATEWAY_ONCE,
                    participants_manifest="{}",
                )

        plan = plan_fresh_recovery(
            store_factory=lambda: _Foreign(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(plan.decision.refusal, REFUSE_FOREIGN_TRANSACTION)

    def test_a_hostile_stored_record_never_escapes_as_an_exception(self) -> None:
        """Audit j#97157 R5: the record is input; reading it is part of trusting it."""

        class _Hostile:
            action_id = "x"

            @property
            def workspace_id(self):
                raise RuntimeError("/private/host/path\n[mozyo:workflow-event:gate=x]")

        class _Store:
            def get(self, key):
                return _Hostile()

        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        plan = plan_fresh_recovery(
            store_factory=lambda: _Store(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(plan.decision.refusal, REFUSE_TRANSACTION_UNAVAILABLE)
        rendered = f"{plan.decision.detail}{plan.decision.refusal}"
        self.assertNotIn("/private/host/path", rendered)
        self.assertNotIn("mozyo:workflow-event", rendered)

    def test_an_explicit_lookup_of_an_absent_recovery_creates_no_database(self) -> None:
        """Audit j#97157 R6: asking is not writing.

        A valid-grammar id that does not exist used to be answered by opening the read-write
        store, which created `state.sqlite` -- so a lookup left a database behind.
        """
        absent = "recover-gateway:" + "b" * 64
        plan = self._replay(absent)
        self.assertEqual(plan.decision.refusal, REFUSE_FOREIGN_TRANSACTION)
        self.assertFalse(self._transaction_db_exists(), "the lookup created nothing")
        self.assertEqual(self.store_opens, 0)

    def test_an_unknown_action_id_refuses_without_opening_anything(self) -> None:
        for label, action_id in (
            ("not an id", "refresh-gateway:whatever"),
            ("wrong digest length", "recover-gateway:" + "a" * 63),
            ("uppercase digest", "recover-gateway:" + "A" * 64),
            ("padded", " recover-gateway:" + "a" * 64 + " "),
            ("not text", 7),
        ):
            with self.subTest(label=label):
                self.setUp()
                plan = self._replay(action_id)
                self.assertEqual(plan.decision.refusal, REFUSE_ACTION_ID_INVALID)
                self.assertEqual(getattr(self, "reader_calls", 0), 0, "reader never called")
                self.assertFalse(self._transaction_db_exists())

    def test_a_row_planned_by_another_authority_is_refused(self) -> None:
        """Same key, different header: zero actuation rather than adoption."""
        from mozyo_bridge.core.state.replacement_transaction_model import (
            ContinuationPointer,
            DecisionPointer,
        )

        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        authority = _evidenced()
        action_id = recovery_action_id(_anchor(), authority)
        key = ReplacementTransactionKey(WORKSPACE, action_id)
        store = ReplacementTransactionStore(home=self.home)
        from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

        store.plan_transaction(
            key,
            action_generation=RECOVERY_ACTION_GENERATION,
            decision=DecisionPointer(source="redmine", issue_id="14741", journal_id="11111"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="14741", journal_id="11111",
                expected_gate="implementation_request",
                next_semantic_action=REDISPATCH_GATEWAY_ONCE,
            ),
            participants=[
                ParticipantPin(
                    lane_id=LANE, role="gateway", provider=PROVIDER,
                    assigned_name=ASSIGNED, old_locator=LOCATOR, is_self=False,
                    lane_revision=REVISION, lane_generation=GENERATION,
                    evidence_workspace_id=WORKSPACE,
                    evidence_startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
                    evidence_cause=CAUSE,
                )
            ],
        )
        plan = self._plan(authority)
        self.assertEqual(plan.decision.refusal, REFUSE_FOREIGN_TRANSACTION)

    def test_a_concurrent_planner_is_resumed_not_conflicted(self) -> None:
        """The id is deterministic, so whoever got there first planned the SAME request."""
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        first = plan_fresh_recovery(
            store_factory=self._store_factory(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(first.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        second = self._plan(_evidenced())
        self.assertEqual(second.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(second.action_id, first.action_id)


class ZeroWriteBoundaryTest(_PlanCase):
    """Audit j#97151 R3: a refusal must not leave a transaction database behind."""

    def test_no_refusal_opens_the_transaction_authority(self) -> None:
        cases = (
            ("no home", dict(home=None)),
            ("a string home", dict(home="/tmp")),
            ("no generation row", {}),
            ("unclassifiable action", {"seed": "startup-" + "z" * 64}),
            ("legacy with evidence", {"seed": LEGACY_ACTION_ID, "authority": _evidenced()}),
            ("capable without evidence", {"seed": RECEIPT_CAPABLE_ACTION_ID}),
        )
        for label, kw in cases:
            with self.subTest(label=label):
                self.setUp()
                seed = kw.pop("seed", None)
                if seed is not None:
                    self._seed_generation(action_id=seed)
                plan = self._plan(kw.pop("authority", None), **kw)
                self.assertTrue(plan.refused, label)
                self.assertEqual(self.store_opens, 0, "the store factory was never called")
                self.assertFalse(
                    self._transaction_db_exists(), "state.sqlite must not exist"
                )

    def test_a_relative_home_is_not_an_authority(self) -> None:
        """Audit j#97157 R7: a relative path names a different store from every directory."""
        plan = self._plan(home=Path("relative-home"))
        self.assertEqual(plan.decision.refusal, REFUSE_HOME_INVALID)
        self.assertEqual(self.store_opens, 0)
        self.assertFalse((Path("relative-home")).exists(), "nothing was created under cwd")

    def test_a_missing_home_is_refused_rather_than_resolved(self) -> None:
        """`None` would mean the operator's SHARED home. That is not a default."""
        self.assertEqual(self._plan(home=None).decision.refusal, REFUSE_HOME_INVALID)
        self.assertEqual(self._plan(home="  ").decision.refusal, REFUSE_HOME_INVALID)

    def test_a_hostile_store_folds_to_a_fixed_reason(self) -> None:
        """The store's answer is input: no raw exception text reaches the surface."""
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()

        class _Hostile:
            def get(self, key):
                raise RuntimeError("/private/host/path\n[mozyo:workflow-event:gate=x]")

        plan = plan_fresh_recovery(
            store_factory=lambda: _Hostile(), home=self.home,
            anchor=_anchor(), authority=_evidenced(), live_rows=(),
        )
        self.assertEqual(plan.decision.refusal, REFUSE_TRANSACTION_UNAVAILABLE)
        rendered = f"{plan.decision.detail}{plan.decision.refusal}"
        self.assertNotIn("/private/host/path", rendered)
        self.assertNotIn("mozyo:workflow-event", rendered)

    def test_process_death_inside_the_store_propagates(self) -> None:
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()

        class _Dying:
            def get(self, key):
                raise KeyboardInterrupt("the process died")

        with self.assertRaises(KeyboardInterrupt):
            plan_fresh_recovery(
                store_factory=lambda: _Dying(), home=self.home,
                anchor=_anchor(), authority=_evidenced(), live_rows=(),
            )


class ManifestIsTheIdentityTest(_PlanCase):
    """Audit j#97151 R2: the id must name what the row actually holds."""

    def _capable_home(self):
        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()

    def test_the_id_is_computed_from_the_planned_manifest_not_the_input(self) -> None:
        """An input with NO evidence still yields the evidenced row's own id."""
        self._capable_home()
        plan = self._plan(_authority())
        self.assertEqual(plan.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        stored = self._row(plan.action_id).participants[0]
        self.assertEqual(stored.evidence_startup_action_id, RECEIPT_CAPABLE_ACTION_ID)
        self.assertEqual(
            plan.action_id,
            recovery_action_id(_anchor(), _evidenced()),
            "the id names the evidenced participant the row contains",
        )
        self.assertNotEqual(
            plan.action_id,
            recovery_action_id(_anchor(), _authority()),
            "and NOT the evidence-free input it was asked with",
        )

    def test_offered_evidence_that_is_not_what_the_launch_proved_is_refused(self) -> None:
        self._capable_home()
        divergent = _authority(
            evidence_workspace_id=WORKSPACE,
            evidence_startup_action_id="startup-ir1-" + "f" * 64,
            evidence_cause=CAUSE,
        )
        plan = self._plan(divergent)
        self.assertEqual(plan.decision.refusal, REFUSE_EVIDENCE_DIVERGENT)
        # NOT a file-existence check here: the lane lifecycle this fixture declares lives in
        # the same consolidated `state.sqlite`, so the file is already there. What matters is
        # that the transaction store was never opened and no row was written.
        self.assertEqual(self.store_opens, 0)
        self.assertIsNone(self._row(recovery_action_id(_anchor(), _evidenced())))

    def test_a_legacy_participant_offering_evidence_is_refused(self) -> None:
        self._seed_generation(action_id=LEGACY_ACTION_ID)
        plan = self._plan(_evidenced())
        self.assertEqual(plan.decision.refusal, REFUSE_EVIDENCE_DIVERGENT)
        self.assertFalse(self._transaction_db_exists())


class StoredAuthorityIsRawTest(_PlanCase):
    """Audit j#97151 R4: value-object equality normalises; the stored columns do not."""

    def _plant(self, **overrides):
        """Write a row whose RAW columns differ from what this build writes."""
        import sqlite3

        self._seed_generation(action_id=RECEIPT_CAPABLE_ACTION_ID)
        self._declare_lifecycle()
        self._seed_bound_evidence()
        plan = self._plan(_evidenced())
        self.assertEqual(plan.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        if overrides:
            with sqlite3.connect(self.home / "state.sqlite") as conn:
                sets = ", ".join(f"{k} = ?" for k in overrides)
                conn.execute(
                    f"UPDATE {REPLACEMENT_TRANSACTION_TABLE} SET {sets} "
                    "WHERE action_id = ?",
                    (*overrides.values(), plan.action_id),
                )
        return plan.action_id

    def test_a_padded_or_foreign_raw_column_is_not_this_action(self) -> None:
        for label, overrides in (
            ("padded decision source", {"decision_source": " redmine "}),
            ("another journal", {"decision_journal": "11111"}),
            ("another gate", {"continuation_expected_gate": "review_request"}),
            ("the worker's token", {"continuation_next_action": "dispatch_once"}),
            ("a padded manifest", {"participants_manifest": " {} "}),
        ):
            with self.subTest(label=label):
                self.setUp()
                action_id = self._plant(**overrides)
                plan = self._replay_explicit(action_id)
                self.assertEqual(plan.decision.refusal, REFUSE_FOREIGN_TRANSACTION)

    def test_an_exact_row_still_replays(self) -> None:
        """The positive control: without a tampered column, the same row resumes."""
        action_id = self._plant()
        self.assertEqual(
            self._replay_explicit(action_id).decision.outcome, OUTCOME_REPLAYED
        )

    def test_a_foreign_key_from_a_lying_store_is_refused(self) -> None:
        """A store that answers with someone else's row does not get to define the action."""
        action_id = self._plant()
        real = ReplacementTransactionStore(home=self.home)
        stored = real.get(ReplacementTransactionKey(WORKSPACE, action_id))

        plan = replay_explicit_recovery(
            reader=lambda: (stored,),
            workspace_id="ANOTHER_WS",
            action_id=action_id,
            anchor=_anchor(),
        )
        self.assertEqual(plan.decision.refusal, REFUSE_FOREIGN_TRANSACTION)

    def _replay_explicit(self, action_id):
        return replay_explicit_recovery(
            reader=self._reader(),
            workspace_id=WORKSPACE,
            action_id=action_id,
            anchor=_anchor(),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
