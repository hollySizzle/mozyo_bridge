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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E402,E501
    RECOVERY_ACTION_GENERATION,
    plan_vanished_gateway_recovery,
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
        for axis, value in (
            ("source", "gitlab"),
            ("issue_id", "99999"),
            ("journal_id", "00000"),
        ):
            with self.subTest(axis=axis):
                self.assertNotEqual(
                    recovery_action_id(_anchor(**{axis: value}), _authority()), baseline
                )

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


class _PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)

    def _plan(self, authority=None, anchor=None):
        return plan_vanished_gateway_recovery(
            store=self.store,
            home=self.home,
            anchor=anchor or _anchor(),
            authority=authority or _authority(),
        )

    def _seed_generation(self, action_id=LEGACY_ACTION_ID, **kw):
        base = dict(
            workspace_id=WORKSPACE, lane_id=LANE, role="gateway",
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

    def _seed_bound_evidence(self) -> None:
        key = GenerationKey(
            workspace_id=WORKSPACE, lane_id=LANE, provider=PROVIDER,
            assigned_name=ASSIGNED, startup_action_id=RECEIPT_CAPABLE_ACTION_ID,
        )
        store = LaunchIdentityReceiptStore(home=self.home)
        store.reserve(key, identity_digest=DIGEST)
        store.finalize(
            key, identity_digest=DIGEST, locator=LOCATOR,
            lane_generation=GENERATION, lifecycle_revision=REVISION, composite_proof=True,
        )
        store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )

    def _row(self, action_id):
        return self.store.get(ReplacementTransactionKey(WORKSPACE, action_id))


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
    def test_the_refusal_matrix_writes_nothing(self) -> None:
        cases = (
            ("no generation row", None, {}, REFUSE_GENERATION_UNAVAILABLE),
            ("pending row", LEGACY_ACTION_ID, {"attested": False}, REFUSE_GENERATION_MISMATCH),
            ("another workspace", LEGACY_ACTION_ID, {"workspace_id": "OTHER"}, REFUSE_GENERATION_MISMATCH),
            ("another lane", LEGACY_ACTION_ID, {"lane_id": "issue_other"}, REFUSE_GENERATION_MISMATCH),
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

    def test_a_replay_reads_no_authority_at_all(self) -> None:
        """Past the plan the manifest is authority: the world may have moved on (j#97121)."""
        first = self._plan_capable()
        (self.home / "herdr-launch-generation.sqlite").unlink()
        (self.home / "launch-identity-receipt.sqlite").unlink()
        again = self._plan(_evidenced())
        self.assertEqual(again.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(again.action_id, first.action_id)

    def test_a_row_planned_by_another_authority_is_refused(self) -> None:
        """Same key, different header: zero actuation rather than adoption."""
        from mozyo_bridge.core.state.replacement_transaction_model import (
            ContinuationPointer,
            DecisionPointer,
        )

        authority = _evidenced()
        action_id = recovery_action_id(_anchor(), authority)
        key = ReplacementTransactionKey(WORKSPACE, action_id)
        from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

        self.store.plan_transaction(
            key,
            action_generation=RECOVERY_ACTION_GENERATION,
            decision=DecisionPointer(source="redmine", issue_id="14741", journal_id="00001"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="14741", journal_id="00001",
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
        peer = ReplacementTransactionStore(home=self.home)
        first = plan_vanished_gateway_recovery(
            store=peer, home=self.home, anchor=_anchor(), authority=_evidenced()
        )
        self.assertEqual(first.decision.outcome, OUTCOME_RECEIPT_PLANNED)
        second = self._plan(_evidenced())
        self.assertEqual(second.decision.outcome, OUTCOME_REPLAYED)
        self.assertEqual(second.action_id, first.action_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
