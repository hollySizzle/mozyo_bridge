"""Redmine #15748 — the ``success_owed`` startup strand (and why ``health_check`` is not).

#15712 unstuck the ``rollback_owed`` strand with a receipt-proof-gated read-side
acceptance and left the other two settle-time phases as an out-of-scope residual
(j#108292 / j#108333). This issue resolves ``success_owed`` and records, with evidence,
why ``health_check`` cannot be resolved the same way.

**No phase here is a health verdict.** ``settle`` is driven by
``SessionStartResult.owes_rollback``, deliberately narrower than ``not ok``: an adopted
or read-only surfaced slot that is unhealthy — or a failed ratio / column check — leaves
``ok`` False while owing nothing, so the run takes the ``not owed`` branch anyway. Even
``completed_success`` therefore means only "this run recorded that it owes no
fresh-launch compensation" (review j#108919, verdict j#108925).
``StrandsAreReachableAndAreNotHealthClaims`` pins that directly.

**The acceptance line is whether the compensation DECISION is recorded.**
``rollback_owed`` (debt recorded) and ``success_owed`` (no debt recorded — the same
decision ``completed_success`` carries, minus the final write) are post-decision and lend
their token under the receipt-proof gate. ``health_check`` precedes that decision: the
owning run may still be about to record a debt, so the phase is simultaneously a strand
and the in-flight state of a live launch, and no durable fact separates them.

That distinction is load-bearing because **this verifier is not delivery-scoped**.
``TheDestructiveCloseAuthorityBoundary`` pins the measured reason (review j#108936,
verdict j#108943): ``herdr_offline_rollout_close`` gates its destructive close pins on
this same token with no phase check of its own, so admitting a pre-decision phase hands a
still-launching pair to a close rail. Those tests also pin, deliberately and visibly, the
one destructive-close widening this issue DOES make — ``success_owed`` — so it is a
stated position rather than an accident.

``health_check`` therefore stays fail-closed and its strand stays UNRESOLVED here
(``TheUnresolvedHealthCheckStrand``). Resolving it needs a delivery-scoped capability
separated from this shared verifier plus an explicit amendment to the mid-startup rule —
a design decision routed to the coordinator, not a widening made in a review round.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_close import (  # noqa: E501
    OfflineRolloutCloseExecutor,
    capture_close_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    ACTIONABLE_PHASES,
    REASON_CONDITIONAL_CLOSE_UNAVAILABLE,
    REASON_NOTHING_OWED,
    run_session_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    StartupTransaction,
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
    HEALTH_HEALTHY,
    HEALTH_PROVIDER_EXITED,
)

# The #15712 regression module owns the durable-store fixtures and the real-binding
# composition harness for exactly this join. Reusing them (rather than re-deriving a
# second, subtly different fixture) is what keeps the two issues' pins comparable: the
# ONLY difference between a #15712 case and a #15748 case below is the action phase.
# Only helpers are imported, so no #15712 TestCase is collected twice.
from tests.regressions.test_issue_15712_l1_callback_receipt import (  # noqa: E402
    COORD_LOCATOR,
    COORD_TERMINAL,
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

#: The phases written once the run has RECORDED whether it owes a compensation, while
#: its books are still open. Both lend their token under the receipt-proof gate;
#: `rollback_owed` was admitted by #15712, `success_owed` is what this issue adds.
POST_DECISION_PHASES = (PHASE_ROLLBACK_OWED, PHASE_SUCCESS_OWED)
#: Everything before that decision. `health_check` belongs here even though every launch
#: of the action is already recorded: `settle` writes it BEFORE the compensation branch.
PRE_DECISION_PHASES = (PHASE_PLANNED, PHASE_LAUNCHING, PHASE_HEALTH_CHECK)


def _proven(home, **kw) -> str:
    """A post-decision action with every other conjunct satisfied. Returns the token."""
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


class StrandsAreReachableAndAreNotHealthClaims(unittest.TestCase):
    """Review j#108919 finding_successowedhealthclaim, pinned as executable fact."""

    @staticmethod
    def _unhealthy_but_owing_nothing() -> SessionStartResult:
        """A run that is NOT ok yet owes no compensation.

        The launched slot came up healthy; the ADOPTED sibling did not. ``ok`` folds the
        pair, ``owes_rollback`` folds only this run's own fresh launches — the #13933 R13
        distinction — so this run reaches ``settle(owed=False)`` while the pair is
        unusable.
        """
        return SessionStartResult(
            workspace_id=WS,
            lane_id=LANE,
            slots=[
                SlotResult(
                    provider=ROLE,
                    assigned_name=NAME,
                    outcome="launched",
                    locator=LOCATOR,
                    health=HEALTH_HEALTHY,
                    compensation=COMPENSATION_NOT_NEEDED,
                ),
                SlotResult(
                    provider="codex",
                    assigned_name="adopted-sibling",
                    outcome="adopted",
                    locator="w:9",
                    health=HEALTH_PROVIDER_EXITED,
                    compensation=COMPENSATION_NOT_NEEDED,
                ),
            ],
        )

    def test_a_run_that_is_not_ok_can_still_owe_nothing(self):
        result = self._unhealthy_but_owing_nothing()
        self.assertFalse(result.ok)
        self.assertFalse(result.owes_rollback)

    def _settle(self, home, *, effect_fence=None, completion_fence=None):
        """Drive the REAL settle with the real fences, at the real `owed` value."""
        result = self._unhealthy_but_owing_nothing()
        fence = StartupTransactionFence(home=home)
        transaction = StartupTransaction(
            fence=fence,
            unit=StartupUnit(workspace_id=WS, lane_id=LANE, providers=(ROLE,)),
            nonce="nonce-15748-settle",
            effect_fence=effect_fence,
            completion_fence=completion_fence,
        )
        token = transaction.reserve()
        transaction.record_prepared_pane(
            role=ROLE, assigned_name=NAME, locator=LOCATOR, receipt=_receipt()
        )
        with self.assertRaises(RuntimeError):
            transaction.settle(owed=result.owes_rollback, launched=True)
        return fence.read(token)

    def test_a_completion_fence_failure_strands_the_action_at_health_check(self):
        # The offline-rollout restore path is the reachable caller: it is the only one
        # that supplies a completion fence, and a fence that refuses mid-settle leaves
        # the action holding no compensation decision at all — which is exactly why the
        # phase cannot be accepted (see TheDestructiveCloseAuthorityBoundary).
        home = _tmp()

        def refuse():
            raise RuntimeError("the restore effect edge is no longer admitted")

        self.assertEqual(
            self._settle(home, completion_fence=refuse).phase, PHASE_HEALTH_CHECK
        )

    def test_an_effect_fence_failure_strands_the_action_at_success_owed(self):
        home = _tmp()
        calls = []

        def refuse_after_success_owed():
            calls.append(1)
            # The effect edges settle crosses: reserve (1), settle entry (2), before
            # `success_owed` (3), before the terminal write (4). Refusing the last one
            # leaves the books open at exactly `success_owed`.
            if len(calls) >= 4:
                raise RuntimeError("the restore effect edge is no longer admitted")

        self.assertEqual(
            self._settle(home, effect_fence=refuse_after_success_owed).phase,
            PHASE_SUCCESS_OWED,
        )

    def test_neither_strand_asserts_pair_health(self):
        # Both strands above were produced by a run whose pair was NOT ok. Nothing about
        # either phase may be read as an all-healthy claim.
        self.assertFalse(self._unhealthy_but_owing_nothing().ok)
        self.assertIn(PHASE_SUCCESS_OWED, POST_DECISION_PHASES)
        self.assertIn(PHASE_HEALTH_CHECK, PRE_DECISION_PHASES)


class SuccessOwedStrandDelivery(unittest.TestCase):
    """The strand no rail can settle now lends its launch token."""

    def test_the_strand_yields_the_delivery_token(self):
        home = _tmp()
        token = _proven(home)
        self.assertEqual(_delivery_token(home), token)

    def test_the_strand_delivers_through_the_real_production_binding(self):
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
        # The binding carries the seeded strand's action token, so the token came from
        # the real durable join and not from a synthetic fixture value.
        self.assertEqual(binding["startup_action_id"], action_id, msg=out)
        self.assertEqual(binding["locator"], COORD_LOCATOR, msg=out)

    def test_the_strand_delivers_through_the_standard_callback_rail(self):
        # NOTE on what this does and does not measure (#15712 j#108301): the standard
        # rail's identity gate is route resolution + locator probe + startup admission
        # and does NOT read the launch-generation store, so this leg pins the callback
        # composition for a strand fixture — it is not evidence of the acceptance. The
        # acceptance is proven by the queue-enter leg above.
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="standard",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_SUCCESS_OWED
            ),
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        self.assertEqual(_outcome_from(out).get("status"), "sent", msg=out)
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


class StrandRefusalBoundaries(unittest.TestCase):
    """Everything the widened acceptance must NOT admit stays fail-closed.

    Every boundary runs against BOTH post-decision phases: a gate that held for one and
    not the other is the exact defect the single `receipt_gated_phases` set prevents.
    """

    def _refused(self, **kw):
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase, **kw)
                self.assertEqual(_delivery_token(home), "")

    def test_without_the_receipt_proof_it_stays_refused(self):
        # A caller that cannot bind the participant receipt to the current terminal
        # (recovery / destructive-close direct callers) keeps the strict
        # completed_success-only behavior — the same asymmetry #15712 established.
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase)
                self.assertEqual(_bare_token(home), "")

    def test_a_receipt_minted_for_a_replacement_terminal_stays_refused(self):
        self._refused(receipt=_receipt(terminal_id="terminal-B"))

    def test_a_receipt_minted_for_another_slot_name_stays_refused(self):
        self._refused(receipt=_receipt(name="other-slot"))

    def test_a_missing_attestation_event_stays_refused(self):
        self._refused(attestation_event_participant=None)

    def test_a_foreign_participants_attestation_event_stays_refused(self):
        self._refused(attestation_event_participant="other-slot")

    def test_a_closed_participant_stays_refused(self):
        self._refused(closed=True)

    def test_a_foreign_locator_participant_stays_refused(self):
        self._refused(locator="w:OTHER")

    def test_a_pending_generation_stays_refused(self):
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                token = _seed_action(home, phase=phase)
                _seed_generation(home, token, finalize=False)
                self.assertEqual(_delivery_token(home), "")

    def test_a_replacement_live_terminal_stays_refused(self):
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase)
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
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase)
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
        # A SECOND, equally well-proven strand action exists for the same slot. The
        # generation row names exactly one launch, and only that one's token is lent:
        # the launch token stays collision-free per launch, and an ambiguous store never
        # resolves to "some action that would qualify".
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                named = _seed_action(home, phase=phase)
                other = _seed_sibling_action(home, phase=phase)
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
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                token = _proven(home, phase=phase)
                self.assertEqual(_delivery_token(home), token)
                StartupTransactionFence(home=home).set_phase(
                    token, PHASE_COMPLETED_ROLLED_BACK
                )
                self.assertEqual(_delivery_token(home), "")


class PreDecisionPhasesStayFailClosed(unittest.TestCase):
    """`planned` / `launching` / `health_check`: the compensation decision is not in."""

    def test_a_pre_decision_phase_is_refused_with_every_other_conjunct_satisfied(self):
        for phase in PRE_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                _proven(home, phase=phase)
                self.assertEqual(_delivery_token(home), "")
                self.assertEqual(_bare_token(home), "")

    def test_a_pre_decision_phase_zero_sends_through_the_real_production_binding(self):
        for phase in PRE_DECISION_PHASES:
            with self.subTest(phase=phase):
                result, herdr, _action, out, err = _run_coordinator_callback(
                    mode="queue-enter",
                    get_states=["idle"],
                    wait_results=[(0, "")],
                    seed=lambda home, ws, name, _phase=phase: _seed_coordinator_proof(
                        home, ws, name, phase=_phase
                    ),
                )
                self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
                outcome = _outcome_from(out)
                self.assertEqual(outcome.get("status"), "blocked", msg=out)
                self.assertEqual(outcome.get("reason"), "target_unavailable", msg=out)
                self.assertFalse(_injections(herdr), msg=herdr.sends)

    def test_a_rolled_back_action_stays_refused(self):
        home = _tmp()
        _proven(home, phase=PHASE_COMPLETED_ROLLED_BACK)
        self.assertEqual(_delivery_token(home), "")


class TheAcceptanceIsReadOnly(unittest.TestCase):
    """No mutation authority is introduced: repeated acceptance never writes."""

    def test_repeated_acceptance_is_idempotent_and_leaves_the_action_untouched(self):
        for phase in POST_DECISION_PHASES:
            with self.subTest(phase=phase):
                home = _tmp()
                token = _proven(home, phase=phase)
                fence = StartupTransactionFence(home=home)
                before = fence.read(token)
                for _ in range(3):
                    self.assertEqual(_delivery_token(home), token)
                after = fence.read(token)
                self.assertEqual(after.phase, phase)
                self.assertEqual(after.revision, before.revision)
                self.assertEqual(after.updated_at, before.updated_at)
                self.assertEqual(
                    after.as_authority_payload(), before.as_authority_payload()
                )


# ---------------------------------------------------------------------------------------
# The destructive consumer of the SAME token (review j#108936). `capture_close_authority`
# and `_present_pin_join` gate offline-rollout close pins on
# `verified_terminal_generation_token` alone — neither re-checks the action phase — so any
# phase this verifier admits becomes destructive-close authority.
# ---------------------------------------------------------------------------------------

CLOSE_NAME = "mzb1_ws_codex_default"
CLOSE_TERMINAL = "terminal-close-current"
CLOSE_LOCATOR = "w1:p1"


def _close_agent(*, locator: str = CLOSE_LOCATOR) -> HerdrObservedAgent:
    return HerdrObservedAgent(
        name=CLOSE_NAME,
        managed=True,
        workspace_id="ws",
        lane_id="default",
        role="codex",
        runtime_state="awaiting_input",
        locator=locator,
        terminal_id=CLOSE_TERMINAL,
    )


def _close_view(*agents: HerdrObservedAgent) -> HerdrInventoryView:
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment="ws",
        agents=agents or (_close_agent(),),
        raw_row_count=len(agents) or 1,
        invalid_row_count=0,
    )


def _close_pane_rows(view: HerdrInventoryView) -> tuple[dict, ...]:
    return tuple(
        {
            "pane_id": agent.locator,
            "workspace_id": agent.locator.split(":", 1)[0],
            "tab_id": f"{agent.locator.split(':', 1)[0]}:t1",
            "terminal_id": agent.terminal_id,
            "agent": agent.role,
        }
        for agent in view.agents
    )


_CLOSE_PLAN = {
    "agents": [
        {
            "assigned_name": CLOSE_NAME,
            "workspace_id": "ws",
            "lane_id": "default",
            "provider": "codex",
        }
    ],
    "phase_order": [
        {"phase": "non_top_workspace_stop", "assigned_names": []},
        {"phase": "top_workspace_stop", "assigned_names": [CLOSE_NAME]},
    ],
}


def _seed_close_authority(home: Path, phase: str) -> str:
    """Seed the full destructive-close join (v4 attestation + generation + action)."""
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id="ws", lane_id="default", providers=("codex",)),
        "nonce-15748-close",
    )
    token = action.action_id
    fence.record_participant(
        token,
        Participant(
            role="codex",
            assigned_name=CLOSE_NAME,
            locator=CLOSE_LOCATOR,
            receipt=pane_bound_receipt(
                target_workspace="w1",
                target_tab="w1:t1",
                native_name=native_name_for(CLOSE_NAME),
                terminal_id=CLOSE_TERMINAL,
            ),
        ),
    )
    assert append_execution_event(
        fence, token, STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=CLOSE_NAME
    )
    if phase == PHASE_COMPLETED_SUCCESS:
        fence.set_phase(token, PHASE_SUCCESS_OWED)
    fence.set_phase(token, phase)
    from tests.support.current_launch_authority import seed_current_generation

    seed_current_generation(
        home,
        workspace_id="ws",
        lane_id="default",
        role="codex",
        assigned_name=CLOSE_NAME,
        locator=CLOSE_LOCATOR,
        action_id=token,
        terminal_id=CLOSE_TERMINAL,
    )
    HerdrIdentityAttestationStore(home=home).upsert(
        IdentityAttestationRecord(
            assigned_name=CLOSE_NAME,
            workspace_id="ws",
            role="codex",
            lane_id="default",
            locator=CLOSE_LOCATOR,
            terminal_id=CLOSE_TERMINAL,
            verdict="present",
            observed_at="2026-07-15T12:00:00+00:00",
        )
    )
    return token


class TheDestructiveCloseAuthorityBoundary(unittest.TestCase):
    """Which phases may license an offline-rollout destructive close (j#108936)."""

    def _capture(self, phase: str):
        home = Path(tempfile.mkdtemp()) / "home"
        token = _seed_close_authority(home, phase)
        return capture_close_authority(
            view=_close_view(), plan=_CLOSE_PLAN, home=home
        ), home, token

    def test_health_check_never_licenses_a_destructive_close(self):
        # THE finding. `settle` writes this phase before the compensation branch, so a
        # pre-decision pair — possibly a launch still in flight — must never reach the
        # close rail. Typed refusal, zero pins.
        captured, _home, _token = self._capture(PHASE_HEALTH_CHECK)
        self.assertFalse(captured.ok, captured)
        self.assertEqual(captured.reason, "close_authority_generation_unverified")
        self.assertNotIn("close_authority", captured.receipt)

    def test_the_other_pre_decision_and_rolled_back_phases_never_license_a_close(self):
        for phase in (PHASE_LAUNCHING, PHASE_COMPLETED_ROLLED_BACK):
            with self.subTest(phase=phase):
                captured, _home, _token = self._capture(phase)
                self.assertFalse(captured.ok, captured)
                self.assertNotIn("close_authority", captured.receipt)

    def test_success_owed_does_license_a_close_and_that_is_the_stated_position(self):
        # This IS a destructive-authority widening introduced by #15748, recorded here
        # deliberately rather than left as an incidental side effect: `success_owed`
        # carries the same recorded compensation decision as `completed_success`, so the
        # close rail sees the same fact it would have seen one write later.
        captured, _home, token = self._capture(PHASE_SUCCESS_OWED)
        self.assertTrue(captured.ok, captured)
        pins = captured.receipt["close_authority"]["pins"]
        self.assertEqual([pin["startup_action_id"] for pin in pins], [token])

    def test_rollback_owed_licensing_is_the_pre_existing_15712_position(self):
        captured, _home, token = self._capture(PHASE_ROLLBACK_OWED)
        self.assertTrue(captured.ok, captured)
        pins = captured.receipt["close_authority"]["pins"]
        self.assertEqual([pin["startup_action_id"] for pin in pins], [token])

    def test_completed_success_licensing_is_unchanged(self):
        captured, _home, token = self._capture(PHASE_COMPLETED_SUCCESS)
        self.assertTrue(captured.ok, captured)
        pins = captured.receipt["close_authority"]["pins"]
        self.assertEqual([pin["startup_action_id"] for pin in pins], [token])

    def _executor(self, home, state):
        return OfflineRolloutCloseExecutor(
            home=home,
            env={},
            inventory_reader=lambda: state["view"],
            pane_inventory_reader=lambda: _close_pane_rows(state["view"]),
            workspace_paths={"ws": str(home / "repo")},
            settle_timeout=0,
            poll_interval=0,
        )

    def _drifted_close(self, drift_to: str):
        """Capture at an admitted phase, drift the action, then try to close."""
        captured, home, _token = self._capture(PHASE_SUCCESS_OWED)
        self.assertTrue(captured.ok, captured)
        fence = StartupTransactionFence(home=home)
        token = captured.receipt["close_authority"]["pins"][0]["startup_action_id"]
        fence.set_phase(token, drift_to)
        state = {"view": _close_view()}
        action = {
            "private_bindings": {
                "workspace_paths": {"ws": str(home / "repo")},
                "agents": _CLOSE_PLAN["agents"],
                "close_authority": captured.receipt["close_authority"],
            },
            "plan": _CLOSE_PLAN,
            "completed_phases": [],
        }
        target = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_"
            "handoff.application.sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target, return_value=SimpleNamespace(failed=False)) as close:
            result = self._executor(home, state).close_names(
                action=action, names=(CLOSE_NAME,), replaying=False
            )
        return result, close

    def test_the_close_executor_refuses_a_pin_whose_action_drifted_pre_decision(self):
        # Even with a pin already captured, the executor re-derives the join from the
        # same verifier before every close. A phase that drifted back before the
        # compensation decision is a typed zero-effect refusal (IR step 6 phase drift).
        result, close = self._drifted_close(PHASE_HEALTH_CHECK)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.reason, "close_authority_generation_drift")
        close.assert_not_called()

    def test_the_close_executor_refuses_a_pin_whose_action_was_rolled_back(self):
        result, close = self._drifted_close(PHASE_COMPLETED_ROLLED_BACK)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.reason, "close_authority_generation_drift")
        close.assert_not_called()


class _LiveNoConditionalCloseOps:
    """The current Herdr provider: a live agent row and no conditional-close primitive."""

    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.closed = []

    def agent_rows(self):
        return list(self.rows)

    def runtime_state(self, _locator):
        return "awaiting_input"

    def observe_composer(self, _locator):
        return True, False

    def startup_blocker(self, _provider, _locator):
        return ""

    def open_obligations(self, _workspace_id, _assigned_names):
        return []

    def supports_conditional_close(self):
        return False

    def close_agent_participant(self, *, workspace_id, lane_id, target):
        self.closed.append(target)
        return True, ""

    def close_prepared_pane(self, *, locator, workspace_id, tab_id, expected_terminal_id=""):
        self.closed.append(locator)
        return True, ""

    def current_generation_targets_absent(self, action, targets, *, store_home):
        return False

    def prepared_pane(self, *, locator, workspace_id, tab_id, expected_terminal_id=""):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
            PREPARED_PANE_PRESENT,
            PreparedPaneObservation,
        )

        return PreparedPaneObservation(
            state=PREPARED_PANE_PRESENT,
            locator=locator,
            workspace_id=workspace_id,
            tab_id=tab_id,
            terminal_id=expected_terminal_id,
        )


class TheUnresolvedHealthCheckStrand(unittest.TestCase):
    """`health_check` is left UNRESOLVED here, and this measures why (j#108943).

    Round 2 accepted the phase into the shared verifier and had to withdraw it: the same
    token licenses a destructive close. So the strand keeps the property that motivated
    the issue — no rail can settle it on this runtime — and the remedy (a delivery-scoped
    capability split out of the shared verifier, plus an explicit amendment to the
    mid-startup rule) is routed to the coordinator as a design decision, not decided
    here. These pins keep the residual honest instead of letting it look resolved.
    """

    @staticmethod
    def _live_rollback(home, phase):
        token = _seed_coordinator_proof(home, WS, NAME, phase=phase)
        fence = StartupTransactionFence(home=home)
        ops = _LiveNoConditionalCloseOps(
            [
                {
                    "name": NAME,
                    "pane_id": COORD_LOCATOR,
                    "terminal_id": COORD_TERMINAL,
                    "agent": ROLE,
                    "agent_status": "idle",
                    "native_name": native_name_for(NAME),
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                }
            ]
        )
        return run_session_rollback(
            action_id=token, ops=ops, fence=fence, execute=True
        ), ops, fence, token

    def test_a_live_health_check_strand_cannot_be_terminalized_on_this_runtime(self):
        result, ops, fence, token = self._live_rollback(_tmp(), PHASE_HEALTH_CHECK)
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason, REASON_CONDITIONAL_CLOSE_UNAVAILABLE)
        self.assertEqual(ops.closed, [])
        self.assertEqual(fence.read(token).phase, PHASE_HEALTH_CHECK)

    def test_a_live_health_check_strand_still_cannot_receive_a_governed_handoff(self):
        # The residual, stated as the user-visible symptom rather than as a phase fact.
        home = _tmp()
        _proven(home, phase=PHASE_HEALTH_CHECK)
        self.assertEqual(_delivery_token(home), "")

    def test_a_success_owed_strand_is_not_even_claimed_by_the_rail(self):
        # The complementary gap the acceptance DOES close: `success_owed` is outside
        # ACTIONABLE_PHASES, so the rail answers `nothing_owed` without looking at all.
        result, ops, fence, token = self._live_rollback(_tmp(), PHASE_SUCCESS_OWED)
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason, REASON_NOTHING_OWED)
        self.assertEqual(ops.closed, [])
        self.assertEqual(fence.read(token).phase, PHASE_SUCCESS_OWED)

    def test_the_rollback_rail_still_claims_the_phases_it_claimed_before(self):
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

    def test_completed_success_still_needs_no_receipt_proof(self):
        # The terminal phase is the one that lends its token on the participant join
        # alone; every post-decision open-books phase needs the receipt gate.
        home = _tmp()
        token = _seed_action(home, phase=PHASE_HEALTH_CHECK)
        fence = StartupTransactionFence(home=home)
        fence.set_phase(token, PHASE_SUCCESS_OWED)
        fence.set_phase(token, PHASE_COMPLETED_SUCCESS)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)
        self.assertEqual(_bare_token(home), token)

    def test_the_restored_pair_repin_is_not_widened(self):
        # #15769's write-side re-attest admits completed_success / rollback_owed only.
        # This issue changed the READ side, so the write-side admission must be
        # byte-identical — `success_owed` and `health_check` still refuse.
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
