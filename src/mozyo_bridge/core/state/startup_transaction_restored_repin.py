"""Restored-pair participant re-pin for the startup fence (Redmine #15769).

Companion to :mod:`mozyo_bridge.core.state.startup_transaction_fence` (the same
delegation shape as :mod:`.startup_action_capability`): the fence class exposes
:meth:`StartupTransactionFence.repin_restored_participant` and delegates the body
here.

A Herdr/tmux server loss can restore the SAME launched process under a NEW
server-owned terminal id and possibly a NEW pane locator. The participant row then
still records the launch-time locator and the launch-time ``pane_bound_v2`` receipt
(whose ``terminal=`` field names the OLD terminal), so two read-side conjuncts —
deliberately unchanged (design decision #15769 j#108766) — can never hold again:

* ``completed_generation_startup_token``'s ``participant.locator ==
  generation.locator`` equality, once the generation row is re-attested to the live
  locator; and
* the ``participant_receipt_matches`` proof supplied by
  ``verified_terminal_generation_token`` (the production queue-enter delivery
  conjunct), which requires the receipt's terminal to equal the CURRENT live
  terminal.

This is the participant-side half of the governed write-side re-attest: only the
restored-pair rebind rail calls it, after proving the identity join on server-owned
inventory facts, and it records the old->new lineage durably alongside.

**Receipt re-mint reasoning (#15769 round 2).** The receipt's role in the read-side
conjunct is to prove "the current terminal is this launch's own side effect". After
a server restore that statement is re-derived, not re-observed: the restore
preserved the process, and the rail's server-owned join (unique live named slot,
SLOT_LIVE, exact stamps, unique canonical terminal) is the evidence that the NEW
terminal hosts the SAME launch. The rail therefore re-mints the receipt with the
live terminal taken from the server inventory — never from a caller — while the
container fields (workspace / tab / native name) are carried byte-identically from
the launch receipt, and the swap is CAS-guarded on the byte-exact OLD receipt. A
participant whose receipt is absent, unparseable, v1 (never terminal-bound — the
spec forbids promoting v1 provenance to v2), foreign-named, or bound to a terminal
that is neither the generation row's old terminal nor the live one is refused: a
receipt that cannot be re-proven from server-owned facts is never fabricated.

This remains a deliberate, BOUNDED exception to the fence's "terminal phase is
written once" rule, stated rather than slipped in: it is field-scoped (only
``locator`` and ``receipt``; every identity field stays byte-identical),
CAS-guarded (the exact expected old locator — and, when the receipt moves, the
exact expected old receipt bytes — are required), and admitted only for the two
phases the read-side launch proof can accept (``completed_success`` /
``rollback_owed``). A mid-startup action still belongs to the launch rails and
refuses; a rolled-back action has nothing to re-pin. For a live-preserved
``rollback_owed`` action the re-pin also keeps the rollback debt pointed at the
pane the process actually occupies, instead of a recycled pane id a foreign
process could later claim.
"""

from __future__ import annotations

from typing import Optional


def repin_restored_participant(
    fence,
    action_id: str,
    role: str,
    *,
    assigned_name: str,
    expected_locator: str,
    new_locator: Optional[str] = None,
    expected_receipt: Optional[str] = None,
    new_receipt: Optional[str] = None,
):
    """CAS-move ONE participant's ``locator`` and/or ``receipt`` (module docstring).

    ``new_locator`` moves the locator (requires it to differ from
    ``expected_locator``); ``expected_receipt`` + ``new_receipt`` swap the receipt
    (byte-exact CAS on the old bytes; the receipt strings are opaque tokens here —
    the RAIL derives the new one from server-owned facts, this core module never
    parses or invents provenance). At least one axis must change.
    """
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
    if not (wanted_role and name and old_locator):
        raise StartupTransactionError(
            "participant re-pin requires the exact role, assigned name, and expected "
            "old locator; refusing a blank identity axis"
        )
    if old_locator != expected_locator:
        raise StartupTransactionError(
            "participant re-pin refuses whitespace-wrapped locators; the stored "
            "bytes are exact and are never normalized to match"
        )
    moved_locator = old_locator
    if new_locator is not None:
        moved_locator = _norm(new_locator)
        if not moved_locator or moved_locator != new_locator:
            raise StartupTransactionError(
                "participant re-pin refuses a blank or whitespace-wrapped new locator"
            )
        if moved_locator == old_locator:
            raise StartupTransactionError(
                "participant re-pin refused: the expected and new locators are "
                "identical (nothing moved; the caller reports a typed no-op)"
            )
    if (expected_receipt is None) != (new_receipt is None):
        raise StartupTransactionError(
            "participant receipt re-mint requires BOTH the exact expected old "
            "receipt and the new receipt; refusing a half-specified swap"
        )
    if new_receipt is not None:
        if not isinstance(expected_receipt, str) or not isinstance(new_receipt, str):
            raise StartupTransactionError(
                "participant receipt re-mint requires exact receipt strings"
            )
        if not expected_receipt or not new_receipt or new_receipt == expected_receipt:
            raise StartupTransactionError(
                "participant receipt re-mint refused: blank or identical receipts "
                "(nothing to re-prove; the caller reports a typed no-op)"
            )
    if new_locator is None and new_receipt is None:
        raise StartupTransactionError(
            "participant re-pin refused: no axis to move (the caller reports a "
            "typed no-op)"
        )
    with fence._hold():
        action = fence._require(action_id)
        if action.phase not in (PHASE_COMPLETED_SUCCESS, PHASE_ROLLBACK_OWED):
            raise StartupTransactionError(
                f"startup action {action_id!r} is {action.phase!r}; a restored-pair "
                "participant re-pin is admitted only for completed_success / "
                "rollback_owed"
            )
        participant = action.participant_for(wanted_role)
        if (
            participant is None
            or participant.closed
            or participant.assigned_name != name
            or participant.locator != old_locator
            or (new_receipt is not None and participant.receipt != expected_receipt)
        ):
            raise StartupTransactionError(
                f"startup action {action_id!r} has no open {wanted_role!r} "
                "participant matching this exact assigned name, expected locator, "
                "and expected receipt; refusing to re-pin a foreign, closed, or "
                "already-moved participant"
            )
        updated = tuple(
            Participant(
                role=p.role,
                assigned_name=p.assigned_name,
                locator=moved_locator if p is participant else p.locator,
                receipt=(
                    new_receipt
                    if (p is participant and new_receipt is not None)
                    else p.receipt
                ),
                closed=p.closed,
            )
            for p in action.participants
        )
        fence._write(action_id, phase=action.phase, participants=updated)
        return fence._require(action_id)


__all__ = ("repin_restored_participant",)
