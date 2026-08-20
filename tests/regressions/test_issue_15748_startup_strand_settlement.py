"""Redmine #15748 — ``success_owed`` / ``health_check`` startup strand settlement.

#15712 unstuck the ``rollback_owed`` strand with a receipt-proof-gated read-side
acceptance and recorded the other two settle-time phases as an out-of-scope residual
(j#108292 / j#108333). This issue decides each of them on its own evidence (design
journal j#108902):

* ``success_owed`` is accepted under the SAME receipt-proof gate. ``settle`` writes it
  on the ``owed=False`` branch alone, so the record durably says "the probe reported
  all-healthy, this run owes no compensation, only the terminal success record is
  outstanding" — a strictly STRONGER statement than the ``rollback_owed`` debt #15712
  already admits. It is also absent from the rollback rail's actionable set, so nothing
  can close the pane out from under an accepted token, and nothing can settle it either
  (the rail answers ``nothing_owed``): a run that died between its two final phase
  writes is otherwise permanently unprovable.
* ``health_check`` stays refused, and that refusal is now an explicit decision rather
  than an unexamined residual. ``settle`` writes it BEFORE the health branch, so it
  carries no verdict at all, and every healthy launch passes through it — a strand and
  an in-flight probe are indistinguishable from anything durable. Its canonical
  settlement path remains the explicit public rollback rail, which already claims it.

No write path is introduced: the acceptance is read-side only, so the fence's store,
lock, CAS and phase-transition rules are untouched (pinned below).
"""

from __future__ import annotations

import unittest

from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_PLANNED,
    PHASE_ROLLBACK_OWED,
    PHASE_SUCCESS_OWED,
    Participant,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    ACTIONABLE_PHASES,
)

# The #15712 regression module owns the durable-store fixtures and the real-binding
# composition harness for exactly this join. Reusing them (rather than re-deriving a
# second, subtly different fixture) is what keeps the two issues' pins comparable: the
# ONLY difference between a #15712 case and a #15748 case below is the action phase.
# Only helpers are imported, so no #15712 TestCase is collected twice.
from tests.regressions.test_issue_15712_l1_callback_receipt import (  # noqa: E402
    COORD_LOCATOR,
    LANE,
    LOCATOR,
    NAME,
    ROLE,
    TERMINAL_ID,
    WS,
    _bare_token,
    _delivery_token,
    _injections,
    _outcome_from,
    _receipt,
    _run_coordinator_callback,
    _seed_action,
    _seed_coordinator_proof,
    _seed_generation,
    _send_texts,
    _tmp,
)


def _proven(home, **kw) -> str:
    """A ``success_owed`` action with every other conjunct satisfied. Returns the token."""
    kw.setdefault("phase", PHASE_SUCCESS_OWED)
    token = _seed_action(home, **kw)
    _seed_generation(home, token)
    return token


def _seed_sibling_action(home, *, phase: str) -> str:
    """A SECOND fully-proven action for the same slot, under a different launch nonce.

    ``reserve`` derives the action id from (canonical unit, nonce), so a distinct nonce
    is what makes this a distinct launch rather than a replay of the first one.
    """
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=(ROLE,)),
        "nonce-15748-sibling",
    )
    token = action.action_id
    fence.record_participant(
        token,
        Participant(role=ROLE, assigned_name=NAME, locator=LOCATOR, receipt=_receipt()),
    )
    assert append_execution_event(
        fence, token, STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=NAME
    )
    fence.set_phase(token, phase)
    return token


class SuccessOwedStrandDelivery(unittest.TestCase):
    """The strand the rollback rail cannot settle now lends its launch token."""

    def test_success_owed_strand_yields_the_delivery_token(self):
        home = _tmp()
        token = _proven(home)
        self.assertEqual(_delivery_token(home), token)

    def test_success_owed_delivers_through_the_real_production_binding(self):
        # The production consumer's own join (`observe_queue_enter_gateway_binding` ->
        # `verified_terminal_generation_token`), unpatched, against real SQLite stores.
        result, herdr, action_id, out, err = _run_coordinator_callback(
            mode="queue-enter",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_SUCCESS_OWED
            ),
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        self.assertEqual(len(_send_texts(herdr)), 1, msg=herdr.sends)
        binding = outcome["queue_enter_turn_start_observation"]["gateway_binding"]
        # The binding carries the seeded success_owed action token, so the token came
        # from the real durable join and not from a synthetic fixture value.
        self.assertEqual(binding["startup_action_id"], action_id, msg=out)
        self.assertEqual(binding["locator"], COORD_LOCATOR, msg=out)

    def test_success_owed_delivers_through_the_standard_callback_rail(self):
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="standard",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_SUCCESS_OWED
            ),
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(len(_send_texts(herdr)), 1, msg=herdr.sends)
        self.assertEqual(
            len([op for op in herdr.sends if op[0] == "send_keys"]), 1, msg=herdr.sends
        )
        self.assertEqual(
            {op[1] for op in _injections(herdr)}, {COORD_LOCATOR}, msg=herdr.sends
        )

    def test_a_busy_receiver_still_refuses_before_injection(self):
        # Accepting the strand's proof does not touch the admission layer: busy stays
        # `precondition_not_idle` with zero injection (ADR-0002's normal path).
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="standard",
            get_states=["working"],
            wait_results=[(0, "")],
            seed=lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_SUCCESS_OWED
            ),
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "precondition_not_idle", msg=out)
        self.assertFalse(_injections(herdr), msg=herdr.sends)


class SuccessOwedRefusalBoundaries(unittest.TestCase):
    """Everything the widened acceptance must NOT admit stays fail-closed."""

    def test_without_the_receipt_proof_it_stays_refused(self):
        # A caller that cannot bind the participant receipt to the current terminal
        # (recovery / destructive-close direct callers) keeps the strict
        # completed_success-only behavior — the same asymmetry #15712 established.
        home = _tmp()
        _proven(home)
        self.assertEqual(_bare_token(home), "")

    def test_a_receipt_minted_for_a_replacement_terminal_stays_refused(self):
        home = _tmp()
        _proven(home, receipt=_receipt(terminal_id="terminal-B"))
        self.assertEqual(_delivery_token(home), "")

    def test_a_receipt_minted_for_another_slot_name_stays_refused(self):
        home = _tmp()
        _proven(home, receipt=_receipt(name="other-slot"))
        self.assertEqual(_delivery_token(home), "")

    def test_a_missing_attestation_event_stays_refused(self):
        home = _tmp()
        _proven(home, attestation_event_participant=None)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_participants_attestation_event_stays_refused(self):
        home = _tmp()
        _proven(home, attestation_event_participant="other-slot")
        self.assertEqual(_delivery_token(home), "")

    def test_a_closed_participant_stays_refused(self):
        home = _tmp()
        _proven(home, closed=True)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_locator_participant_stays_refused(self):
        home = _tmp()
        _proven(home, locator="w:OTHER")
        self.assertEqual(_delivery_token(home), "")

    def test_a_pending_generation_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, phase=PHASE_SUCCESS_OWED)
        _seed_generation(home, token, finalize=False)
        self.assertEqual(_delivery_token(home), "")

    def test_a_replacement_live_terminal_stays_refused(self):
        home = _tmp()
        _proven(home)
        self.assertEqual(
            verified_terminal_generation_token(
                home,
                assigned_name=NAME,
                workspace_id=WS,
                role=ROLE,
                lane_id=LANE,
                locator=LOCATOR,
                terminal_id="terminal-B",
            ),
            "",
        )

    def test_a_foreign_workspace_query_stays_refused(self):
        home = _tmp()
        _proven(home)
        self.assertEqual(
            verified_terminal_generation_token(
                home,
                assigned_name=NAME,
                workspace_id="wsOTHER",
                role=ROLE,
                lane_id=LANE,
                locator=LOCATOR,
                terminal_id=TERMINAL_ID,
            ),
            "",
        )

    def test_a_second_strand_actions_token_is_never_borrowed(self):
        # A SECOND, equally well-proven success_owed action exists for the same slot.
        # The generation row names exactly one launch, and only that one's token is
        # lent: the launch token stays collision-free per launch, and an ambiguous
        # store never resolves to "some action that would qualify".
        home = _tmp()
        named = _seed_action(home, phase=PHASE_SUCCESS_OWED)
        other = _seed_sibling_action(home, phase=PHASE_SUCCESS_OWED)
        self.assertNotEqual(named, other)
        _seed_generation(home, named)
        self.assertEqual(_delivery_token(home), named)

    def test_a_generation_row_naming_an_absent_action_stays_refused(self):
        # The finalized row is byte-perfect, but the token it names has no action in
        # this store: an unreadable / absent join lends nothing.
        home = _tmp()
        _seed_generation(home, "startup-does-not-exist")
        self.assertIsNone(
            StartupTransactionFence(home=home).read("startup-does-not-exist")
        )
        self.assertEqual(_delivery_token(home), "")

    def test_a_later_rollback_revokes_the_token_on_the_next_read(self):
        # The verdict is re-derived from the store on every call — nothing is cached,
        # so a phase that moves under a concurrent rail flips the answer immediately.
        home = _tmp()
        token = _proven(home)
        self.assertEqual(_delivery_token(home), token)
        StartupTransactionFence(home=home).set_phase(token, PHASE_COMPLETED_ROLLED_BACK)
        self.assertEqual(_delivery_token(home), "")


class TheAcceptanceIsReadOnly(unittest.TestCase):
    """No mutation authority is introduced: repeated acceptance never writes."""

    def test_repeated_acceptance_is_idempotent_and_leaves_the_action_untouched(self):
        home = _tmp()
        token = _proven(home)
        fence = StartupTransactionFence(home=home)
        before = fence.read(token)
        for _ in range(3):
            self.assertEqual(_delivery_token(home), token)
        after = fence.read(token)
        self.assertEqual(after.phase, PHASE_SUCCESS_OWED)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.as_authority_payload(), before.as_authority_payload())


class HealthCheckStaysFailClosed(unittest.TestCase):
    """The explicit #15748 decision: health_check records no verdict, so it lends none."""

    def test_health_check_with_every_other_conjunct_satisfied_stays_refused(self):
        home = _tmp()
        _proven(home, phase=PHASE_HEALTH_CHECK)
        self.assertEqual(_delivery_token(home), "")
        self.assertEqual(_bare_token(home), "")

    def test_the_remaining_mid_startup_phases_stay_refused(self):
        for phase in (PHASE_PLANNED, PHASE_LAUNCHING, PHASE_HEALTH_CHECK):
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase)
                self.assertEqual(_delivery_token(home), "")

    def test_health_check_zero_sends_through_the_real_production_binding(self):
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="queue-enter",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_HEALTH_CHECK
            ),
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "target_unavailable", msg=out)
        self.assertFalse(_injections(herdr), msg=herdr.sends)

    def test_the_rollback_rail_owns_health_check_and_not_success_owed(self):
        # This is the evidence behind the split decision, pinned to the code: the
        # public rollback rail already claims `health_check` (so that strand HAS a
        # canonical settlement path and needs no new one), while `success_owed` has
        # none (so the read side is its only honest remedy).
        self.assertIn(PHASE_HEALTH_CHECK, ACTIONABLE_PHASES)
        self.assertIn(PHASE_LAUNCHING, ACTIONABLE_PHASES)
        self.assertIn(PHASE_ROLLBACK_OWED, ACTIONABLE_PHASES)
        self.assertNotIn(PHASE_SUCCESS_OWED, ACTIONABLE_PHASES)


class PriorInvariantsUnchanged(unittest.TestCase):
    """#15712 and #15769 boundaries this change must not move."""

    def test_rollback_owed_acceptance_is_unchanged(self):
        home = _tmp()
        token = _proven(home, phase=PHASE_ROLLBACK_OWED)
        self.assertEqual(_delivery_token(home), token)
        self.assertEqual(_bare_token(home), "")

    def test_completed_rolled_back_stays_refused(self):
        home = _tmp()
        _proven(home, phase=PHASE_COMPLETED_ROLLED_BACK)
        self.assertEqual(_delivery_token(home), "")

    def test_completed_success_still_needs_no_receipt_proof(self):
        # The terminal phase is the line: it lends its token on the participant join
        # alone, while every settled-but-uncleared phase needs the receipt gate.
        home = _tmp()
        token = _seed_action(home, phase=PHASE_HEALTH_CHECK)
        fence = StartupTransactionFence(home=home)
        fence.set_phase(token, PHASE_SUCCESS_OWED)
        fence.set_phase(token, PHASE_COMPLETED_SUCCESS)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)
        self.assertEqual(_bare_token(home), token)

    def test_the_restored_pair_repin_is_not_widened_to_the_strand_phases(self):
        # #15769's write-side re-attest admits completed_success / rollback_owed only.
        # This issue changed the READ side, so the write-side admission must be
        # byte-identical — success_owed and health_check still refuse.
        for phase in (PHASE_SUCCESS_OWED, PHASE_HEALTH_CHECK):
            with self.subTest(phase=phase):
                home = _tmp()
                token = _seed_action(home, phase=phase)
                fence = StartupTransactionFence(home=home)
                with self.assertRaises(StartupTransactionError):
                    fence.repin_restored_participant(
                        token,
                        ROLE,
                        assigned_name=NAME,
                        expected_locator=LOCATOR,
                        new_locator="w:MOVED",
                    )

    def test_the_restored_pair_repin_still_admits_the_two_accepted_write_phases(self):
        for phase in (PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED):
            with self.subTest(phase=phase):
                home = _tmp()
                token = _seed_action(home, phase=PHASE_HEALTH_CHECK)
                fence = StartupTransactionFence(home=home)
                if phase == PHASE_COMPLETED_SUCCESS:
                    fence.set_phase(token, PHASE_SUCCESS_OWED)
                fence.set_phase(token, phase)
                moved = fence.repin_restored_participant(
                    token,
                    ROLE,
                    assigned_name=NAME,
                    expected_locator=LOCATOR,
                    new_locator="w:MOVED",
                )
                self.assertEqual(moved.participant_for(ROLE).locator, "w:MOVED")
                self.assertEqual(moved.phase, phase)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
