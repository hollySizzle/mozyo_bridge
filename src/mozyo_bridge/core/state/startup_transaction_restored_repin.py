"""Restored-pair participant locator re-pin for the startup fence (Redmine #15769).

Companion to :mod:`mozyo_bridge.core.state.startup_transaction_fence` (the same
delegation shape as :mod:`.startup_action_capability`): the fence class exposes
:meth:`StartupTransactionFence.repin_restored_participant_locator` and delegates
the body here.

A Herdr/tmux server loss can restore the SAME launched process at a NEW pane
locator. The participant row then still records the launch-time locator, and the
launch-proof acceptance (``completed_generation_startup_token``:
``participant.locator == generation.locator``) — deliberately unchanged (design
decision #15769 j#108766) — can never hold again once the generation row is
re-attested to the live locator. This is the participant-side half of that
governed write-side re-attest: only the restored-pair rebind rail calls it, after
proving the identity join on server-owned inventory facts, and it records the
old->new lineage durably alongside.

This is a deliberate, BOUNDED exception to the fence's "terminal phase is written
once" rule, stated rather than slipped in: it is field-scoped (only ``locator``;
the ``receipt`` — the launch-time pane evidence — and every identity field stay
byte-identical), CAS-guarded (the exact expected old locator and assigned name are
required), and admitted only for the two phases the read-side launch proof can
accept (``completed_success`` / ``rollback_owed``). A mid-startup action still
belongs to the launch rails and refuses; a rolled-back action has nothing to
re-pin. For a live-preserved ``rollback_owed`` action the re-pin also keeps the
rollback debt pointed at the pane the process actually occupies, instead of a
recycled pane id a foreign process could later claim.
"""

from __future__ import annotations


def repin_restored_participant_locator(
    fence,
    action_id: str,
    role: str,
    *,
    assigned_name: str,
    expected_locator: str,
    new_locator: str,
):
    """CAS-move ONE participant's ``locator`` (contract in the module docstring)."""
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS,
        PHASE_ROLLBACK_OWED,
        Participant,
        StartupTransactionError,
        _norm,
    )

    wanted_role = _norm(role)
    name = _norm(assigned_name)
    old_locator = _norm(expected_locator)
    moved_locator = _norm(new_locator)
    if not (wanted_role and name and old_locator and moved_locator):
        raise StartupTransactionError(
            "participant locator re-pin requires the exact role, assigned name, "
            "expected old locator, and new locator; refusing a blank identity axis"
        )
    if moved_locator != new_locator or old_locator != expected_locator:
        raise StartupTransactionError(
            "participant locator re-pin refuses whitespace-wrapped locators; the "
            "stored bytes are exact and are never normalized to match"
        )
    if moved_locator == old_locator:
        raise StartupTransactionError(
            "participant locator re-pin refused: the expected and new locators are "
            "identical (nothing moved; the caller reports a typed no-op)"
        )
    with fence._hold():
        action = fence._require(action_id)
        if action.phase not in (PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED):
            raise StartupTransactionError(
                f"startup action {action_id!r} is {action.phase!r}; a restored-pair "
                "locator re-pin is admitted only for completed_success / rollback_owed"
            )
        participant = action.participant_for(wanted_role)
        if (
            participant is None
            or participant.closed
            or participant.assigned_name != name
            or participant.locator != old_locator
        ):
            raise StartupTransactionError(
                f"startup action {action_id!r} has no open {wanted_role!r} participant "
                f"matching this exact assigned name and expected locator; refusing to "
                "re-pin a foreign, closed, or already-moved participant"
            )
        updated = tuple(
            Participant(
                role=p.role,
                assigned_name=p.assigned_name,
                locator=moved_locator if p is participant else p.locator,
                receipt=p.receipt,
                closed=p.closed,
            )
            for p in action.participants
        )
        fence._write(action_id, phase=action.phase, participants=updated)
        return fence._require(action_id)


__all__ = ("repin_restored_participant_locator",)
