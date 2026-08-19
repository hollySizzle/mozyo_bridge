"""Read-side launch-generation authority verdicts (Redmine #14203 / #15712).

Companion to :mod:`mozyo_bridge.core.state.herdr_launch_generation`, which owns the
durable store and its 2-phase (``pending`` -> ``attested``) rows. This module owns the
pure read-side question both the queue-enter binding (delivery time) and the gateway
recovery (recovery time) ask of that store: *may this attested row lend its
collision-free launch token right now?* Keeping the verdict in one place is what stops
the two consumers from drifting (Design Answer j#87472); the store module re-exports
these names so every historical import path keeps working.

Imports of the store symbols happen inside the functions, mirroring the store module's
own function-level import style and keeping this companion import-safe from either
direction.
"""

from __future__ import annotations

from pathlib import Path


def completed_generation_startup_token(
    home, generation, *, norm, norm_lane, participant_receipt_matches=None
) -> str:
    """Return the generation token only after exact startup success.

    ``completed_success`` remains the ordinary proof. One additional durable shape is
    accepted (Redmine #15712): a ``rollback_owed`` action whose recorded participant is
    exactly this generation's slot, ONLY when the caller supplies a terminal-bound
    ``participant_receipt_matches`` proof AND the action's execution events carry this
    participant's own ``attestation_write_succeeded``. ``rollback_owed`` records a
    pair-level health-completion debt, not a disproof of the launched terminal's
    identity: on a runtime without a conditional-close primitive the rollback rail
    preserves every present participant, so a slot whose boot outlived the settle-time
    probe (measured: the default-lane coordinator relaunch) stays live, attested, and
    generation-finalized while its action can never reach ``completed_success``.
    Without the receipt proof, or without the participant's own attestation event, or
    for any other phase (``completed_rolled_back`` / mid-startup), the answer stays
    ``""`` exactly as before — callers that cannot prove the terminal keep the strict
    ``completed_success``-only behavior.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import GENERATION_ATTESTED
    from mozyo_bridge.core.state.startup_execution_events import (
        STAGE_ATTESTATION_WRITE_SUCCEEDED, read_execution_events,
    )
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED, StartupTransactionError,
        StartupTransactionFence,
    )

    token = norm(getattr(generation, "startup_action_id", "") or "")
    if norm(getattr(generation, "phase", "")) != GENERATION_ATTESTED or not token:
        return ""
    try:
        fence = StartupTransactionFence(home=home)
        action = fence.read(token)
    except (StartupTransactionError, Exception):  # noqa: BLE001
        return ""
    if action is None:
        return ""
    action_phase = norm(getattr(action, "phase", ""))
    if action_phase not in (PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED):
        return ""
    role = norm(getattr(generation, "role", ""))
    unit = getattr(action, "unit", None)
    if not (
        unit is not None
        and norm(getattr(unit, "workspace_id", ""))
        == norm(getattr(generation, "workspace_id", ""))
        and norm_lane(getattr(unit, "lane_id", ""))
        == norm_lane(getattr(generation, "lane_id", ""))
        and role in tuple(getattr(unit, "providers", ()) or ())
    ):
        return ""
    participant = action.participant_for(role)
    if participant is None or getattr(participant, "closed", True):
        return ""
    if not (
        norm(getattr(participant, "assigned_name", ""))
        == norm(getattr(generation, "assigned_name", ""))
        and norm(getattr(participant, "locator", ""))
        == norm(getattr(generation, "locator", ""))
    ):
        return ""
    if participant_receipt_matches is not None:
        try:
            if not participant_receipt_matches(getattr(participant, "receipt", "")):
                return ""
        except Exception:  # noqa: BLE001 - malformed receipt grants no authority
            return ""
    if action_phase == PHASE_ROLLBACK_OWED:
        # The widened acceptance is receipt-proof-gated: a caller that cannot bind the
        # participant receipt to the current terminal gets no token from a debt-bearing
        # action. The participant's own attestation event re-proves the wrapper attested
        # inside THIS action, so a fabricated generation row cannot borrow a recorded
        # launch it never ran.
        if participant_receipt_matches is None:
            return ""
        events = read_execution_events(fence, token)
        name = norm(getattr(generation, "assigned_name", ""))
        if not any(
            norm(getattr(event, "stage", "")) == STAGE_ATTESTATION_WRITE_SUCCEEDED
            and norm(getattr(event, "participant", "")) == name
            for event in (events or ())
        ):
            return ""
    return token


def verified_generation_token(
    home: Path | None,
    *,
    assigned_name: str,
    workspace_id: str,
    role: str,
    lane_id: str,
    locator: str,
    live_terminal_id: object,
    norm,
    norm_lane,
    participant_receipt_matches=None,
) -> str:
    """The attested generation token for this exact gateway, or ``""`` (read-only, j#87472).

    The ONE generation authority shared by the queue-enter binding (delivery time) and the
    gateway recovery (recovery time), so the two can never drift. Returns the collision-free
    per-launch token (``startup_action_id``) iff, all exact and fail-closed:

    * an ``attested`` generation row exists for ``assigned_name`` with a non-empty token,
      ``verdict == present``, and ``workspace_id`` / ``role`` / ``lane_id`` / ``locator``
      equal to the expected identity (the whole generation is ONE atomic row — identity and
      token are never read from two files that could tear); AND
    * that token names a startup transaction that reached ``completed_success`` — or, only
      under the receipt-proof-gated acceptance documented on
      :func:`completed_generation_startup_token` (Redmine #15712), a live-preserved
      ``rollback_owed`` action — whose participant for ``role`` is exactly this gateway
      (``assigned_name`` + ``locator``, not closed) — a rolled-back / foreign / superseded
      generation never lends its token.

    ``norm`` / ``norm_lane`` are injected by the caller so this core module never imports the
    provider identity helpers (the dependency never points core -> provider). Any unreadable
    / absent / pending / mismatched input yields ``""``.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
    from mozyo_bridge.core.state.herdr_launch_generation import (
        GENERATION_ATTESTED,
        HerdrLaunchGenerationError,
        HerdrLaunchGenerationStore,
    )

    try:
        generation = HerdrLaunchGenerationStore(home=home).read(norm(assigned_name))
    except (HerdrLaunchGenerationError, Exception):  # noqa: BLE001 - unreadable => none
        return ""
    if generation is None:
        return ""
    token = norm(getattr(generation, "startup_action_id", "") or "")
    if not (
        norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and token
        and norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and norm(getattr(generation, "assigned_name", "")) == norm(assigned_name)
        and norm(getattr(generation, "role", "")) == norm(role)
        and norm_lane(getattr(generation, "lane_id", "")) == norm_lane(lane_id)
        and norm(getattr(generation, "locator", "")) == norm(locator)
        and type(live_terminal_id) is str
        and bool(live_terminal_id)
        and live_terminal_id.strip() == live_terminal_id
        and getattr(generation, "terminal_id", "") == live_terminal_id
        and norm(getattr(generation, "workspace_id", "")) == norm(workspace_id)
    ):
        return ""
    return completed_generation_startup_token(
        home,
        generation,
        norm=norm,
        norm_lane=norm_lane,
        participant_receipt_matches=participant_receipt_matches,
    )


__all__ = (
    "completed_generation_startup_token",
    "verified_generation_token",
)
