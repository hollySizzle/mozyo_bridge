"""Read-side launch-generation authority verdicts (Redmine #14203 / #15712 / #15748).

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
    """Return the generation token only for a launch whose terminal is proven ours.

    **What the phases actually say.** None of them is a health verdict, and this
    authority consumes none (review #15748 j#108919 / verdict j#108925). ``settle`` is
    driven by ``SessionStartResult.owes_rollback`` — deliberately narrower than
    ``not ok``: an adopted or read-only surfaced slot that is unhealthy, or a failed
    ratio / column check, leaves ``ok`` False while owing nothing. So
    ``completed_success`` means "this run recorded that it owes no fresh-launch
    compensation", NOT "the pair is healthy", and the same is true of every phase
    below. What this function asks is an IDENTITY question — *is the live terminal at
    this locator this launch's own side effect?* — so a bookkeeping phase is never a
    disproof.

    **The line is whether the launch set is closed**, not whether the phase is
    terminal. ``settle`` is the only writer of ``health_check`` / ``success_owed``, and
    it writes ``health_check`` on entry, once every launch this action will make has
    been made and recorded. From there the action is one of:

    * ``health_check`` (Redmine #15748) — settle was entered; the compensation verdict
      is not recorded yet. A run that died in that window (measured reachable: the
      offline-rollout restore path's ``completion_fence`` raising between the two
      writes) strands here.
    * ``rollback_owed`` (Redmine #15712) — the run recorded a fresh-launch compensation
      debt. On a runtime without a conditional-close primitive the rollback rail
      preserves every present participant, so a slot whose boot outlived the
      settle-time probe (measured: the default-lane coordinator relaunch) stays live,
      attested and generation-finalized while its action can never terminalize.
    * ``success_owed`` (Redmine #15748) — the run recorded that it owes no compensation;
      only the terminal record is outstanding. No rail owes it a settlement at all (the
      rollback rail answers ``nothing_owed``), so a run that died between its two final
      writes is otherwise permanently unprovable.
    * ``completed_success`` — the same statement as ``success_owed``, durably recorded.

    All three settle-entered phases lend their token ONLY under the receipt-proof gate:
    a caller-supplied terminal-bound ``participant_receipt_matches`` proof AND this
    participant's own ``attestation_write_succeeded`` among the action's execution
    events. Terminal ``completed_success`` keeps lending on the participant join alone,
    exactly as before. Accepting a recorded debt (``rollback_owed``, #15712) while
    refusing an unrecorded verdict (``health_check``) would be incoherent for an
    identity question — and none of this takes the rollback rail's authority away: it
    still claims those phases and may still close the panes.

    ``planned`` / ``launching`` stay refused because the launch set is still OPEN there
    — ``record_participant`` can add another role, and rolling the whole run back is
    the normal disposition. ``completed_rolled_back`` stays refused because its
    participants were proven absent. Without the receipt proof, without the
    participant's own attestation event, or for any of those phases, the answer stays
    ``""`` — callers that cannot prove the terminal keep the strict
    ``completed_success``-only behavior.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import GENERATION_ATTESTED
    from mozyo_bridge.core.state.startup_execution_events import (
        STAGE_ATTESTATION_WRITE_SUCCEEDED, read_execution_events,
    )
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS, PHASE_HEALTH_CHECK, PHASE_ROLLBACK_OWED,
        PHASE_SUCCESS_OWED, StartupTransactionError, StartupTransactionFence,
    )

    #: The settle-entered phases the receipt-proof gate admits: every launch of the
    #: action is recorded, but its books are not closed. Kept as one local set so the
    #: gate below can never be applied to only some of them.
    receipt_gated_phases = (
        PHASE_HEALTH_CHECK, PHASE_ROLLBACK_OWED, PHASE_SUCCESS_OWED,
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
    if action_phase not in (PHASE_COMPLETED_SUCCESS,) + receipt_gated_phases:
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
    if action_phase in receipt_gated_phases:
        # The widened acceptance is receipt-proof-gated: a caller that cannot bind the
        # participant receipt to the current terminal gets no token from an action whose
        # books are still open. The participant's own attestation event re-proves the
        # wrapper attested inside THIS action, so a fabricated generation row cannot
        # borrow a recorded launch it never ran.
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
      :func:`completed_generation_startup_token`, a settle-entered ``rollback_owed``
      (Redmine #15712), ``success_owed`` or ``health_check`` (Redmine #15748) action —
      whose participant for ``role`` is exactly this gateway (``assigned_name`` +
      ``locator``, not closed) — a rolled-back / open-launch-set (``planned`` /
      ``launching``) / foreign / superseded generation never lends its token.

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
