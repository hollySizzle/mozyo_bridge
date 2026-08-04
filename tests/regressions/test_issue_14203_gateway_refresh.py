"""Regression: guarded gateway refresh use case (Redmine #14203).

Pins ``sublane recover-gateway``'s execute discipline over the #13806 replacement-transaction
machinery: preflight never writes; every approval axis is fail-closed; the actuation closes
ONLY the exact pinned gateway generation; the resume drives the EXISTING durable anchor
exactly once through the shared continuation-drain authority (idempotency-first, record
attempted before the send, action-time authority re-join, never a blind resend); a stopped
leg holds the durable replay fence and a post-close replay is admitted ONLY on the expected
``identity_unknown`` + a committed-close transaction. Fakes only — no live process, no herdr.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from mozyo_bridge.core.state.herdr_launch_generation import (  # noqa: E402
    HERDR_LAUNCH_GENERATION_FILENAME,
)
from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    LAUNCH_IDENTITY_RECEIPT_FILENAME,
)
from tests.support.current_launch_authority import (  # noqa: E402
    LEGACY_ACTION_ID,
    RECEIPT_CAPABLE_ACTION_ID,
    seed_current_generation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402,E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_continuation_drain import (  # noqa: E402,E501
    CONTINUATION_AUTHORITY_MOVED,
    CONTINUATION_CONFIRMED,
    CONTINUATION_SEND_FAILED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E402,E501
    GatewayRefreshRequest,
    GatewayRefreshUseCase,
    REFRESH_STATUS_COMPLETED,
    REFRESH_STATUS_PREFLIGHT,
    REFRESH_STATUS_REFUSED,
    REFRESH_STATUS_STOPPED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_LAUNCH_AUTHORITY,
    REFRESH_BLOCK_NON_GATEWAY,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    REFRESH_BLOCK_UNKNOWN,
    TURN_CLASS_FAILED,
    TURN_CLASS_UNCONFIRMED,
    TURN_REASON_RATE_LIMIT,
    TURN_REASON_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E402,E501
    LAUNCH_AUTHORITY_OK,
    LAUNCH_AUTHORITY_WORKTREE_MISMATCH,
    LAUNCH_AUTHORITY_WORKTREE_UNBOUND,
    launch_authority_current,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_PRESENT,
)

GEN = 3
FIXED = "2026-07-24T12:00:00+00:00"
GATEWAY = dict(
    lane_id="issue_x_lane", role="codex", provider="codex", assigned_name="gw",
    old_locator="w:3",
)
ACTION_ID = "refresh-gateway:issue_x_lane:codex:codex:gw:w:3:r4"


def _turn(**overrides) -> GatewayTurnObservation:
    facts = dict(
        delivery_confirmed=True, turn_started=True, settled_turn_ended=True,
        expected_gate_absent=True, durable_source_fresh=True,
    )
    facts.update(overrides)
    return GatewayTurnObservation(**facts)


def _target(**overrides) -> GatewayRefreshObservation:
    facts = dict(
        identity_resolved=True, is_lane_implementation_gateway=True,
        issue_lane_matches=True, generation_matches=True, settled_idle=True,
        composer_clear=True, resume_anchor_present=True,
        worker_distinct_preserved=True, no_authority_conflict=True,
        # #14475: the use case JOINS this axis from ``ops.lane_authority_reason`` and
        # overwrites whatever the target observer carried, so this value is inert here —
        # kept green so the canonical builder stays an all-facts-hold observation.
        launch_authority_current=True,
    )
    facts.update(overrides)
    return GatewayRefreshObservation(**facts)


class FakeActuatorPort:
    """A synthetic ExactGenerationActuatorPort — no live process, no DB."""

    def __init__(self):
        self.close_result: dict[tuple, str] = {}
        self.launch_result: dict[tuple, str] = {}
        self.closed: list[tuple] = []
        self.launched: list[tuple[str, tuple]] = []
        self._pres = PreservationObservation(identity_matches=True, attestation_fresh=True)

    def observe_old_slot(self, pin) -> str:
        return OLD_SLOT_PRESENT

    def observe_preservation(self, pin) -> PreservationObservation:
        return self._pres

    def close_exact_generation(self, pin) -> str:
        self.closed.append(pin.identity)
        return self.close_result.get(pin.identity, CLOSE_DONE)

    def launch_action_bound(self, action_id: str, pin) -> str:
        self.launched.append((action_id, pin.identity))
        return self.launch_result.get(pin.identity, LAUNCH_DONE)

    def verify_attestation(self, action_id: str, pin) -> str:
        return ATTEST_BOUND


class FakeGatewayOps:
    """A synthetic GatewayRecoveryOps — fixed observations + a recorded resume rail."""

    def __init__(
        self,
        turn=None,
        target=None,
        *,
        send_result=DRAIN_SEND_OK,
        confirm_after_send=True,
        already_landed=False,
        lane_authority=True,
        name_free=True,
        rail_ready=True,
        approval=True,
    ):
        self._turn = turn if turn is not None else _turn()
        self._target = target if target is not None else _target()
        self.send_result = send_result
        self.confirm_after_send = confirm_after_send
        self.resumes: list = []
        self._landed = already_landed
        self._lane_authority = lane_authority
        self.authority_checks: list = []
        self._name_free = name_free
        self.name_free_checks: list = []
        self._rail_ready = rail_ready
        self._approval = approval

    def approval_verified(self, request, *, journal: str) -> bool:
        return bool(self._approval and journal == request.journal)

    def observe_turn(self, request) -> GatewayTurnObservation:
        return self._turn

    def observe_target(self, request) -> GatewayRefreshObservation:
        return self._target

    def lane_authority_reason(self, request) -> str:
        """The #14475 typed axis, driven by the SAME ``_lane_authority`` script.

        The fake mirrors the live adapter's structure: one evaluator, and
        ``resume_lane_authority`` is its boolean projection. A test that scripts the authority
        moving mid-run therefore moves it for the preflight axis and the action-time fence
        alike, exactly as the real join would.
        """
        self.authority_checks.append(request)
        v = self._lane_authority
        if isinstance(v, list):
            current = v.pop(0) if v else True
        else:
            current = v
        return LAUNCH_AUTHORITY_OK if current else LAUNCH_AUTHORITY_WORKTREE_UNBOUND

    def resume_lane_authority(self, request) -> bool:
        return launch_authority_current(self.lane_authority_reason(request))

    def replacement_store_admission(self, key, pin):
        """No store constraint in this fixture (Redmine #14756 j#96848).

        ``None`` is the admit verdict, so every assertion in this module measures exactly
        what it measured before the pre-close fence existed. The fence's own behaviour is
        measured in ``test_issue_14756_lane_epoch_attestation``.
        """
        return None

    def gateway_name_free_of_live_process(self, request) -> bool:
        self.name_free_checks.append(request)
        return self._name_free

    def resume_rail_ready(self, request) -> bool:
        return self._rail_ready

    def resume_confirmed(self, continuation) -> bool:
        return self._landed

    def resume_once(self, continuation) -> str:
        self.resumes.append(continuation)
        if self.send_result == DRAIN_SEND_OK and self.confirm_after_send:
            self._landed = True
        return self.send_result


class _RefreshCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.workspace_id = "ws"
        self.port = FakeActuatorPort()
        # Ruling j#97105: a refresh reads the participant's CURRENT launch-generation row,
        # and a home without one is not a production lane -- it is a lane whose current
        # authority is missing, which refuses. So the pre-#14741 path is stated as a fact
        # about this exact slot: a canonical untagged `startup-<64hex>` row.
        self._seed_current_authority()

    def _seed_current_authority(self, **overrides):
        base = dict(
            workspace_id=self.workspace_id, lane_id=GATEWAY["lane_id"],
            role=GATEWAY["role"], assigned_name=GATEWAY["assigned_name"],
            locator=GATEWAY["old_locator"],
        )
        base.update(overrides)
        seed_current_generation(self.home, **base)

    def _request(self, **overrides) -> GatewayRefreshRequest:
        base = dict(
            issue="14203", lane=GATEWAY["lane_id"], role=GATEWAY["role"],
            provider=GATEWAY["provider"], assigned_name=GATEWAY["assigned_name"],
            locator=GATEWAY["old_locator"], journal="84223", action_id=ACTION_ID,
            action_generation=GEN, gateway_revision="4",
            lane_revision="5", lane_generation="2",
            resume_anchor_journal="87251", resume_gate="review_request",
        )
        base.update(overrides)
        return GatewayRefreshRequest(**base)

    def _use_case(self, ops):
        return GatewayRefreshUseCase(
            self.store, self.port, ops, workspace_id=self.workspace_id,
            clock=lambda: FIXED,
        )

    def _row(self):
        return self.store.get(ReplacementTransactionKey(self.workspace_id, ACTION_ID))


class PreflightTests(_RefreshCase):
    def test_preflight_classifies_and_writes_nothing(self):
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.status, REFRESH_STATUS_PREFLIGHT)
        self.assertEqual(outcome.turn_class, TURN_CLASS_FAILED)
        self.assertEqual(outcome.verdict, REFRESH_ACTIONABLE)
        self.assertFalse(outcome.executed)
        self.assertFalse(outcome.is_blocked)
        self.assertIn("gate=gateway_recovery_owner_approval", outcome.required_approval_marker)
        self.assertIsNone(self._row())          # zero writes
        self.assertEqual(self.port.closed, [])  # zero closes
        self.assertEqual(ops.resumes, [])       # zero sends

    def test_the_reason_is_normalized_fail_closed(self):
        ops = FakeGatewayOps(turn=_turn(reason_token="429 raw provider text"))
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.turn_reason, TURN_REASON_UNKNOWN)
        ops = FakeGatewayOps(turn=_turn(reason_token=TURN_REASON_RATE_LIMIT))
        outcome = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(outcome.turn_reason, TURN_REASON_RATE_LIMIT)


class ExecuteRefusalTests(_RefreshCase):
    def _refused(self, ops, request, needle: str):
        outcome = self._use_case(ops).run(request, execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn(needle, outcome.detail)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(ops.resumes, [])
        return outcome

    def test_a_not_actionable_target_refuses_with_zero_close(self):
        ops = FakeGatewayOps(target=_target(is_lane_implementation_gateway=False))
        outcome = self._refused(ops, self._request(), "not actionable")
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_NON_GATEWAY)

    def test_an_unconfirmed_turn_refuses_even_with_a_clean_slot(self):
        # The #14219 false-negative made structural: delivered_not_started-shaped evidence
        # (no positive delivery confirmation) NEVER closes a gateway.
        ops = FakeGatewayOps(turn=_turn(delivery_confirmed=False))
        outcome = self._refused(ops, self._request(), "not actionable")
        self.assertEqual(outcome.turn_class, TURN_CLASS_UNCONFIRMED)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_TURN_NOT_FAILED)

    def test_an_incomplete_approval_pointer_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(journal=""), "not a complete Redmine pointer"
        )

    def test_unverified_approval_is_zero_close(self):
        outcome = self._refused(
            FakeGatewayOps(approval=False), self._request(), "owner approval"
        )
        self.assertTrue(outcome.is_blocked)
        self.assertIsNone(self._row())

    def test_a_mismatched_action_id_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(action_id="refresh-gateway:other"),
            "does not match",
        )

    def test_a_non_positive_generation_refuses(self):
        self._refused(FakeGatewayOps(), self._request(action_generation=0), "generation")

    def test_missing_lane_lifecycle_evidence_refuses(self):
        self._refused(FakeGatewayOps(), self._request(lane_revision=""), "lifecycle")

    def test_a_non_resumable_gate_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(resume_gate="bogus_gate"), "not a resumable"
        )

    def test_a_missing_resume_anchor_refuses(self):
        self._refused(
            FakeGatewayOps(), self._request(resume_anchor_journal=""),
            "resume anchor pointer is incomplete",
        )

    def test_a_missing_gateway_revision_refuses_before_any_write(self):
        # Review j#87364 F5: the row revision is a REQUIRED destructive authority component.
        self._refused(
            FakeGatewayOps(),
            self._request(gateway_revision="", action_id="refresh-gateway:x"),
            "exact gateway generation",
        )

    def test_an_unready_resume_rail_refuses_before_any_close(self):
        # Review j#87364 F2: the resume capability is verified BEFORE the destructive close.
        ops = FakeGatewayOps(rail_ready=False)
        outcome = self._refused(ops, self._request(), "resume_rail_unavailable")
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)

    def test_a_diverged_preexisting_row_is_an_authority_conflict(self):
        ops = FakeGatewayOps()
        first = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(first.status, REFRESH_STATUS_COMPLETED)
        # A second authority for the SAME slot at a different resume anchor must refuse.
        ops2 = FakeGatewayOps()
        outcome = self._use_case(ops2).run(
            self._request(resume_anchor_journal="99999"), execute=True
        )
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn("different refresh authority", outcome.detail)
        self.assertEqual(ops2.resumes, [])

    def test_a_stored_triplet_from_another_workspace_is_a_conflict(self):
        """Redmine #14741 j#97121 item 6: adopting stored evidence is not adopting ANY.

        A progressed replay resumes on the triplet the transaction already pinned, which is
        what makes it independent of a rotated generation or a consumed receipt. That must
        not make a foreign manifest adoptable: a triplet naming another workspace names a
        launch in someone else's workspace, and "it was already stored" does not make it
        this action's evidence. Zero actuation.
        """
        key = ReplacementTransactionKey(self.workspace_id, ACTION_ID)
        planned = ParticipantPin(
            lane_id=GATEWAY["lane_id"], role=GATEWAY["role"],
            provider=GATEWAY["provider"], assigned_name=GATEWAY["assigned_name"],
            old_locator=GATEWAY["old_locator"], is_self=False,
            lane_revision="5", lane_generation="2",
            evidence_workspace_id="ANOTHER_WS",
            evidence_startup_action_id="startup-ir1-" + "a" * 64,
            evidence_cause="update_relaunch",
        )
        # The decision / continuation pointers are the use case's own, learned from a run
        # against a throwaway store so this test states a stored-row conflict rather than
        # re-implementing how the pointers are built.
        scratch_home = Path(tempfile.mkdtemp())
        seed_current_generation(
            scratch_home, workspace_id=self.workspace_id, lane_id=GATEWAY["lane_id"],
            role=GATEWAY["role"], assigned_name=GATEWAY["assigned_name"],
            locator=GATEWAY["old_locator"],
        )
        scratch = ReplacementTransactionStore(home=scratch_home)
        GatewayRefreshUseCase(
            scratch, FakeActuatorPort(), FakeGatewayOps(),
            workspace_id=self.workspace_id, clock=lambda: FIXED,
        ).run(self._request(), execute=True)
        reference = scratch.get(key)
        ops = FakeGatewayOps()
        self.store.plan_transaction(
            key, action_generation=GEN, decision=reference.decision,
            continuation=reference.continuation, participants=[planned],
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn("names another workspace", outcome.detail)
        self.assertEqual(ops.resumes, [])
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self.port.launched, [])
        stored = self.store.get(key).participants[0]
        self.assertEqual(stored.evidence_workspace_id, "ANOTHER_WS")


class CurrentLaunchAuthorityTests(_RefreshCase):
    """Ruling j#97105: the participant's own current row is the only capability binding.

    Every case here is zero-effect on refusal -- the planner runs BEFORE the transaction
    plan, so a refused refresh leaves no row, no supersede and no actuation behind.
    """

    def _run(self, ops=None):
        return self._use_case(ops or FakeGatewayOps()).run(self._request(), execute=True)

    def _assert_zero_effect(self, outcome, ops):
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertIn("update evidence planning refused", outcome.detail)
        self.assertIsNone(self._row(), "no transaction row was created")
        self.assertEqual(self.port.closed, [])
        self.assertEqual(self.port.launched, [])
        self.assertEqual(ops.resumes, [])

    def test_a_canonical_legacy_current_row_refreshes_byte_invariantly(self):
        """The positive control the seeded authority buys: unchanged pre-#14741 behaviour."""
        ops = FakeGatewayOps()
        outcome = self._run(ops)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        stored = self._row().participants[0]
        self.assertEqual(
            (
                stored.evidence_workspace_id,
                stored.evidence_startup_action_id,
                stored.evidence_cause,
            ),
            ("", "", ""),
            "a legacy participant carries no evidence, and none was invented for it",
        )

    def test_an_absent_current_row_is_a_typed_zero_effect_refusal(self):
        """`generation_unavailable`: nobody recorded which action this slot belongs to."""
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        ops = FakeGatewayOps()
        self._assert_zero_effect(self._run(ops), ops)

    def test_a_pending_current_row_is_a_typed_zero_effect_refusal(self):
        """A launch that never proved it came up cannot back a replacement's evidence."""
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self._seed_current_authority(attested=False)
        ops = FakeGatewayOps()
        self._assert_zero_effect(self._run(ops), ops)

    def test_a_current_row_for_another_slot_is_a_typed_zero_effect_refusal(self):
        """A row that belongs to someone else is not this participant's authority."""
        for label, override in (
            ("another workspace", dict(workspace_id="OTHER_WS")),
            ("another lane", dict(lane_id="issue_other")),
            ("a recycled pane", dict(locator="w:99")),
        ):
            with self.subTest(label=label):
                self.setUp()
                self.home = Path(tempfile.mkdtemp())
                self.store = ReplacementTransactionStore(home=self.home)
                self._seed_current_authority(**override)
                ops = FakeGatewayOps()
                self._assert_zero_effect(self._run(ops), ops)

    def test_a_receipt_capable_current_row_without_receipts_refuses(self):
        """Capability lives in the action id, so a tagged row cannot fall back to legacy.

        There is no receipt store in this home. A capable action therefore refuses rather
        than being read as "no evidence, so pre-feature" -- the laundering j#96892 closed.
        """
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self._seed_current_authority(action_id=RECEIPT_CAPABLE_ACTION_ID)
        ops = FakeGatewayOps()
        self._assert_zero_effect(self._run(ops), ops)


class ProgressedReplayTests(_RefreshCase):
    """Ruling j#97121: a replay past the close reads the stored manifest, not the world.

    After the close, the current launch generation legitimately rotates and the bound
    evidence is legitimately consumed -- both AFTER this transaction recorded what it acts
    on. Re-reading them to replay an already durable decision would let ordinary external
    progress refuse an action nobody re-authorised, which is the whole finding.
    """

    def _complete_once(self):
        self._use_case(FakeGatewayOps()).run(self._request(), execute=True)
        return self._row()

    def _replay(self):
        rerun_ops = FakeGatewayOps(already_landed=True, target=_target())
        outcome = self._use_case(rerun_ops).run(self._request(), execute=True)
        return outcome, rerun_ops

    def test_a_replay_survives_the_current_generation_row_vanishing(self):
        before = self._complete_once()
        generation_store = self.home / HERDR_LAUNCH_GENERATION_FILENAME
        self.assertTrue(generation_store.exists(), "the seeded authority was really there")
        generation_store.unlink()
        outcome, rerun_ops = self._replay()
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(rerun_ops.resumes, [])
        after = self._row()
        self.assertEqual(after.participants, before.participants, "stored manifest is intact")

    def test_a_replay_survives_a_rotated_generation_for_the_same_slot(self):
        """The successor launch is a NEW generation -- and it is not this action's business."""
        self._complete_once()
        self._seed_current_authority(
            action_id="startup-" + "9f9f9f9f" * 8, locator="w:99"
        )
        outcome, rerun_ops = self._replay()
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(rerun_ops.resumes, [])

    def test_a_replay_survives_an_unreadable_receipt_authority(self):
        """A corrupt receipt store is not consulted at all on a replay."""
        self._complete_once()
        (self.home / LAUNCH_IDENTITY_RECEIPT_FILENAME).write_bytes(b"not a database")
        outcome, rerun_ops = self._replay()
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(rerun_ops.resumes, [])

    def test_a_replay_consults_no_external_authority_at_all(self):
        """The direct statement: zero planner, generation, lifecycle and receipt calls."""
        self._complete_once()
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery as site

        calls = []
        original = site.plan_participants_with_evidence

        def spy(*args, **kwargs):
            calls.append(kwargs.get("lane_id"))
            return original(*args, **kwargs)

        site.plan_participants_with_evidence = spy
        try:
            outcome, _ = self._replay()
        finally:
            site.plan_participants_with_evidence = original
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(calls, [], "the planner was not consulted on a progressed replay")

    def test_a_fresh_run_still_consults_the_planner(self):
        """The positive control for the spy above: the fresh path is unchanged."""
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery as site

        calls = []
        original = site.plan_participants_with_evidence

        def spy(*args, **kwargs):
            calls.append(kwargs.get("lane_id"))
            return original(*args, **kwargs)

        site.plan_participants_with_evidence = spy
        try:
            outcome = self._use_case(FakeGatewayOps()).run(self._request(), execute=True)
        finally:
            site.plan_participants_with_evidence = original
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(calls, [GATEWAY["lane_id"]])


class HappyPathTests(_RefreshCase):
    def test_close_launch_attest_resume_exactly_once(self):
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertTrue(outcome.closed_old_gateway)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(outcome.resume_status, CONTINUATION_CONFIRMED)
        # Exactly the pinned gateway was closed / relaunched — nothing else.
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.assertEqual(self.port.closed, [identity])
        self.assertEqual([pin for _a, pin in self.port.launched], [identity])
        # The launch was bound to THIS refresh action.
        self.assertEqual(self.port.launched[0][0], ACTION_ID)
        # The resume fired exactly once, carrying the EXISTING anchor (never regenerated).
        self.assertEqual(len(ops.resumes), 1)
        continuation = ops.resumes[0]
        self.assertEqual(continuation.journal_id, "87251")
        self.assertEqual(continuation.expected_gate, "review_request")

    def test_a_rerun_after_completion_survives_lost_confirmation_history(self):
        ops = FakeGatewayOps()
        self._use_case(ops).run(self._request(), execute=True)
        # The exact transaction terminal is stronger than a lossy/compacted delivery ledger.
        rerun_ops = FakeGatewayOps(already_landed=False, target=_target())
        outcome = self._use_case(rerun_ops).run(self._request(), execute=True)
        # The transaction is already completed; the drive confirms with ZERO new close/send.
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(rerun_ops.resumes, [])
        self.assertEqual(self.port.closed, [ (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        ) ])  # only the FIRST run's close — no second close

    def test_an_already_landed_resume_completes_with_zero_send(self):
        ops = FakeGatewayOps(already_landed=True)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(ops.resumes, [])  # idempotency-first: never a duplicate delivery


class StoppedLegTests(_RefreshCase):
    def test_a_failed_launch_stops_with_the_replay_fence_held(self):
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        ops = FakeGatewayOps()
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertTrue(outcome.closed_old_gateway)   # the close committed
        self.assertFalse(outcome.fresh_slot_attested)
        self.assertEqual(ops.resumes, [])             # NO resume behind a failed launch
        self.assertIsNotNone(self._row())             # the durable replay fence stands

    def test_a_post_close_replay_is_admitted_only_on_identity_unknown(self):
        # Crash between close and launch: the old gateway is expectedly absent. A replay
        # whose preflight blocks identity_unknown + a committed-close transaction resumes
        # the owed launch/attest/resume; any OTHER blocker stands.
        identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.port.launch_result[identity] = LAUNCH_ERROR
        self._use_case(FakeGatewayOps()).run(self._request(), execute=True)
        del self.port.launch_result[identity]
        # Replay: the pinned old locator no longer resolves -> identity_unknown preflight.
        replay_ops = FakeGatewayOps(target=GatewayRefreshObservation())
        outcome = self._use_case(replay_ops).run(self._request(), execute=True)
        self.assertTrue(outcome.post_close_resume)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(len(replay_ops.resumes), 1)
        # A NON-identity-unknown blocker is a real fence: it refuses even with the txn.
        blocked_ops = FakeGatewayOps(
            target=_target(composer_clear=False), already_landed=True
        )
        blocked = self._use_case(blocked_ops).run(self._request(), execute=True)
        self.assertEqual(blocked.status, REFRESH_STATUS_REFUSED)
        self.assertFalse(blocked.post_close_resume)

    def test_a_failed_resume_send_stops_without_blind_resend(self):
        ops = FakeGatewayOps(send_result=DRAIN_SEND_ERROR)
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_SEND_FAILED)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(len(ops.resumes), 1)  # exactly one attempt — never repeated blind

    def test_an_authority_move_before_the_resume_is_a_typed_zero_send(self):
        # Authority holds for the #14475 pre-close preflight axis AND the launch probe, then
        # MOVES immediately before the resume transport: the attempt is un-recorded and the
        # resume reports authority_moved with ZERO send. The gateway IS closed and relaunched
        # here — this pins the LATE move, which the pre-close fence cannot and must not catch.
        ops = FakeGatewayOps(lane_authority=[True, True, False])
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.resume_status, CONTINUATION_AUTHORITY_MOVED)
        self.assertTrue(outcome.fresh_slot_attested)
        self.assertEqual(ops.resumes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
