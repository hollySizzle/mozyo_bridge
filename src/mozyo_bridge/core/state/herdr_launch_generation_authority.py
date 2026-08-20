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
    """Return the generation token only after exact startup success.

    The rule, in one line: **a terminal ``completed_success`` lends its token on the
    participant join alone; a SETTLED-BUT-UNCLEARED phase lends it only under the
    receipt-proof gate** — a caller-supplied terminal-bound
    ``participant_receipt_matches`` proof AND this participant's own
    ``attestation_write_succeeded`` among the action's execution events. Two phases sit
    on that second line, and both record a bookkeeping debt rather than a disproof of
    the launched terminal's identity:

    * ``rollback_owed`` (Redmine #15712) — a pair-level health-completion debt. On a
      runtime without a conditional-close primitive the rollback rail preserves every
      present participant, so a slot whose boot outlived the settle-time probe
      (measured: the default-lane coordinator relaunch) stays live, attested, and
      generation-finalized while its action can never reach ``completed_success``.
    * ``success_owed`` (Redmine #15748) — the launcher's probe reported all-healthy and
      the run owes NO compensation; only the terminal success record is outstanding
      (``StartupTransaction.settle`` writes this phase on the ``owed=False`` branch
      alone). It is a strictly STRONGER durable statement than ``rollback_owed``, and
      unlike it the phase is not in the rollback rail's actionable set — nothing can
      later close the pane out from under an accepted token — so refusing the stronger
      statement while accepting the weaker one would be incoherent. No rail owes it a
      settlement either: the rollback rail answers ``nothing_owed``, so a run that died
      between its two final phase writes is otherwise permanently unprovable.

    ``health_check`` is deliberately NOT on that line (Redmine #15748). ``settle``
    writes it BEFORE the health branch, so the record carries no verdict at all, and
    every healthy launch passes through it — a strand and an in-flight probe are
    indistinguishable from anything durable here. Accepting it would infer a probe
    result that was never recorded; promoting it write-side would fabricate one (and
    demoting it to ``rollback_owed`` would fabricate a compensation debt that closes
    healthy pairs on a runtime that HAS conditional close). Its canonical settlement
    path stays the explicit public rollback rail, which already claims it.

    Without the receipt proof, or without the participant's own attestation event, or
    for any other phase (``completed_rolled_back`` / ``planned`` / ``launching`` /
    ``health_check``), the answer stays ``""`` exactly as before — callers that cannot
    prove the terminal keep the strict ``completed_success``-only behavior.
    """
    from mozyo_bridge.core.state.herdr_launch_generation import GENERATION_ATTESTED
    from mozyo_bridge.core.state.startup_execution_events import (
        STAGE_ATTESTATION_WRITE_SUCCEEDED, read_execution_events,
    )
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED, PHASE_SUCCESS_OWED,
        StartupTransactionError, StartupTransactionFence,
    )

    #: The settled-but-uncleared phases the receipt-proof gate admits. Kept as one
    #: local set so the gate below can never be applied to only half of them.
    receipt_gated_phases = (PHASE_ROLLBACK_OWED, PHASE_SUCCESS_OWED)

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
      :func:`completed_generation_startup_token`, a settled-but-uncleared
      ``rollback_owed`` (Redmine #15712) or ``success_owed`` (Redmine #15748) action —
      whose participant for ``role`` is exactly this gateway (``assigned_name`` +
      ``locator``, not closed) — a rolled-back / mid-startup / foreign / superseded
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
