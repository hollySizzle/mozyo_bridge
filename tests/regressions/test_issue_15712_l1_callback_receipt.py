"""Redmine #15712 — L2→L1 callback ``target_unavailable`` (receiver generation proof).

Measured live (#15693 j#108152 / j#108159 / j#108167): every ``handoff reply`` to the
idle default-lane coordinator zero-sent as ``target_unavailable`` although role
authority resolved and the live attestation was exactly-one. The join that refused is
the delivery-time launch proof: the coordinator relaunch's startup transaction settled
``rollback_owed`` (the bounded health probe outlived by the Claude boot), and on a
runtime without a conditional-close primitive that debt can never be cleared while the
pane lives — so a live, attested, generation-finalized, receipt-bound pair was
permanently unprovable under the ``completed_success``-only conjunct.

These regressions pin the fix (receipt-proof-gated ``rollback_owed`` acceptance in
``completed_generation_startup_token``) and every refusal boundary it must not widen:
no receipt proof, foreign terminal receipt, missing / foreign attestation event,
rolled-back action, mid-startup phases, closed / foreign participant, pending
generation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
    verified_generation_token,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_ROLLBACK_OWED,
    PHASE_SUCCESS_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)

WS = "wsL1"
ROLE = "claude"
LANE = "default"
LOCATOR = "w:4"
NAME = "coordinator-slot"
TERMINAL_ID = "terminal-L1"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _receipt(*, name: str = NAME, terminal_id: str = TERMINAL_ID) -> str:
    return pane_bound_receipt(
        target_workspace="w1",
        target_tab="w1:t1",
        native_name=native_name_for(name),
        terminal_id=terminal_id,
    )


def _seed_action(
    home: Path,
    *,
    phase: str = PHASE_ROLLBACK_OWED,
    name: str = NAME,
    locator: str = LOCATOR,
    closed: bool = False,
    receipt: str | None = None,
    attestation_event_participant: str | None = NAME,
) -> str:
    """Reserve the action, record the participant, append (or omit) the wrapper's own
    attestation event, and drive the action to ``phase``. Returns the action token."""
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=(ROLE,)),
        "nonce-15712",
    )
    token = action.action_id
    fence.record_participant(
        token,
        Participant(
            role=ROLE,
            assigned_name=name,
            locator=locator,
            receipt=receipt if receipt is not None else _receipt(name=name),
            closed=closed,
        ),
    )
    if attestation_event_participant is not None:
        assert append_execution_event(
            fence,
            token,
            STAGE_ATTESTATION_WRITE_SUCCEEDED,
            participant=attestation_event_participant,
        )
    fence.set_phase(token, phase)
    return token


def _seed_generation(home: Path, token: str, *, finalize: bool = True) -> None:
    store = HerdrLaunchGenerationStore(home=home)
    store.reserve_pending(
        assigned_name=NAME,
        startup_action_id=token,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
    )
    if finalize:
        store.finalize(
            assigned_name=NAME,
            startup_action_id=token,
            workspace_id=WS,
            role=ROLE,
            lane_id=LANE,
            locator=LOCATOR,
            terminal_id=TERMINAL_ID,
            verdict=VERDICT_PRESENT,
            observed_at="2026-08-18T12:55:42+00:00",
        )


def _delivery_token(home: Path) -> str:
    """The queue-enter delivery join: wrapper with the terminal-bound receipt proof."""
    return verified_terminal_generation_token(
        home,
        assigned_name=NAME,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
        locator=LOCATOR,
        terminal_id=TERMINAL_ID,
    )


def _bare_token(home: Path) -> str:
    """The same authority WITHOUT a receipt proof (recovery-style direct caller)."""
    return verified_generation_token(
        home,
        assigned_name=NAME,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
        locator=LOCATOR,
        live_terminal_id=TERMINAL_ID,
        norm=_norm,
        norm_lane=_norm_lane,
    )


class LivePreservedRollbackOwedDelivery(unittest.TestCase):
    """The measured L2→L1 shape: live + attested + receipt-bound, action rollback_owed."""

    def test_live_receipt_bound_rollback_owed_pair_yields_the_delivery_token(self):
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)

    def test_completed_success_acceptance_is_unchanged(self):
        home = _tmp()
        token = _seed_action(home, phase=PHASE_HEALTH_CHECK)
        fence = StartupTransactionFence(home=home)
        fence.set_phase(token, "success_owed")
        fence.set_phase(token, "completed_success")
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)
        self.assertEqual(_bare_token(home), token)


class RollbackOwedRefusalBoundaries(unittest.TestCase):
    """Everything the widened acceptance must NOT admit stays fail-closed."""

    def test_rollback_owed_without_the_receipt_proof_stays_refused(self):
        # A caller that cannot bind the participant receipt to the current terminal
        # (recovery-style direct verified_generation_token) keeps the strict
        # completed_success-only behavior.
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
        self.assertEqual(_bare_token(home), "")

    def test_a_receipt_minted_for_a_replacement_terminal_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, receipt=_receipt(terminal_id="terminal-B"))
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_receipt_minted_for_another_slot_name_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, receipt=_receipt(name="other-slot"))
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_missing_attestation_event_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, attestation_event_participant=None)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_participants_attestation_event_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, attestation_event_participant="other-slot")
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_rolled_back_action_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, phase=PHASE_COMPLETED_ROLLED_BACK)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_mid_startup_phases_stay_refused(self):
        for phase in (PHASE_LAUNCHING, PHASE_HEALTH_CHECK, PHASE_SUCCESS_OWED):
            with self.subTest(phase=phase):
                home = _tmp()
                token = _seed_action(home, phase=phase)
                _seed_generation(home, token)
                self.assertEqual(_delivery_token(home), "")

    def test_a_closed_participant_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, closed=True)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_locator_participant_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, locator="w:OTHER")
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_pending_generation_stays_refused(self):
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token, finalize=False)
        self.assertEqual(_delivery_token(home), "")

    def test_a_replacement_live_terminal_stays_refused(self):
        # The pane at the locator was replaced after the launch: the live terminal no
        # longer equals the generation row's terminal, so no token regardless of phase.
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
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


if __name__ == "__main__":
    unittest.main()
