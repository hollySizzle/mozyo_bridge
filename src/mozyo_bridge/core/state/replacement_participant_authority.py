"""Pure participant-authority questions for a replacement transaction (Redmine #14741).

Split out of :mod:`.replacement_transaction_model` when that module crossed the
module-health ceiling. These belong together and away from the record / CAS machinery: each
one answers "is this stored participant the same authority as the one in front of me, and
what may I carry across from it?", and none of them reads a store.

``ParticipantPin`` is imported inside the single function that constructs one, so the model
module can import these names without an import cycle.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from mozyo_bridge.core.state.lane_lifecycle_model import norm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin



#: The update-evidence triplet, in the one order every consumer reads it in.
_EVIDENCE_FIELDS = (
    "evidence_workspace_id",
    "evidence_startup_action_id",
    "evidence_cause",
)


def supersede_participant_signature(
    pin: "ParticipantPin",
) -> tuple:
    """The immutable-across-supersede signature of a participant. (pure)

    Everything a supersede re-anchor may NOT change: the stable identity ``(lane_id, role,
    provider, assigned_name)`` PLUS ``old_locator`` (the exact live-generation evidence, and the
    token the recovery action-id is derived from), ``is_self`` (self-close ordering) and the
    update-evidence triplet. Only the lane-lifecycle evidence (``lane_revision`` /
    ``lane_generation``) — the mis-bound field the convergence exists to correct — is
    deliberately excluded (Redmine #13806 R2 F1).

    The triplet is in the signature because a supersede corrects a MIS-BOUND LIFECYCLE, not
    a different launch (Redmine #14741 j#97093 decision 6). Leaving it out would have made
    ``empty -> pinned`` a legal re-anchor: a row planned with no evidence could acquire a
    relaunch cause it never observed, through a path whose whole purpose is to change one
    unrelated field.
    """
    lane_id, role, provider, assigned_name = pin.identity
    return (
        lane_id,
        role,
        provider,
        assigned_name,
        pin.old_locator,
        pin.is_self,
    ) + tuple(getattr(pin, name) for name in _EVIDENCE_FIELDS)

def participant_authority_matches(
    stored: Optional["ParticipantPin"], planned: "ParticipantPin"
) -> bool:
    """Is a stored row the SAME participant authority as the one just planned? (pure)

    Phase is the one field the STORE owns — it advances ``close_owed -> launch_owed -> ...``
    as the transaction runs — so it is compared canonically by holding it equal, and every
    other axis must match as a whole :class:`ParticipantPin`, evidence triplet included.

    The call sites this replaces compared a hand-picked list (locator, revision,
    generation). A hand-picked list answers "did the fields I remembered to name change?",
    and the evidence triplet was not on it — so a stored row could carry a different
    startup action or cause than the participant about to be actuated, and the comparison
    would call them the same authority (Redmine #14741 j#97093 decision 5).
    """
    if stored is None:
        return False
    return stored.with_phase(planned.phase) == planned


def participant_with_stored_evidence(
    base: "ParticipantPin", stored: Optional["ParticipantPin"]
) -> "ParticipantPin":
    """``base``, wearing the STORED manifest's update-evidence triplet. (pure)

    The resume authority for a progressed replacement (Redmine #14741 j#97121). Once a
    transaction has moved past ``close_owed``, the current launch generation may have
    rotated, the bound evidence may have been consumed and the receipt store may have
    changed state -- all legitimately, and all of it AFTER this transaction pinned what it
    was acting on. Re-reading those authorities to replay a decision that is already durable
    lets ordinary external progress refuse an action nobody re-authorised.

    So a replay rebuilds the participant from the request exactly as a fresh plan would, and
    then takes its evidence -- and ONLY its evidence -- from the stored manifest. Everything
    else still has to match, which is what makes this a comparison rather than an
    adoption: the caller feeds the result to :func:`participant_authority_matches` against
    the same stored pin, so a request that names a different locator, lifecycle or identity
    still diverges. ``stored is None`` returns ``base`` unchanged, so the caller's own
    "there is no such participant" refusal is the one that fires.
    """
    if stored is None:
        return base
    from mozyo_bridge.core.state.replacement_transaction_model import ParticipantPin

    return ParticipantPin(
        lane_id=base.lane_id,
        role=base.role,
        provider=base.provider,
        assigned_name=base.assigned_name,
        old_locator=base.old_locator,
        is_self=base.is_self,
        lane_revision=base.lane_revision,
        lane_generation=base.lane_generation,
        evidence_workspace_id=stored.evidence_workspace_id,
        evidence_startup_action_id=stored.evidence_startup_action_id,
        evidence_cause=stored.evidence_cause,
        phase=base.phase,
    )


def stored_evidence_is_foreign(
    pin: Optional["ParticipantPin"], *, workspace_id: str
) -> bool:
    """Does this stored participant's evidence name a DIFFERENT workspace? (pure)

    The one evidence check a replay still owes (Redmine #14741 j#97121 item 6). Adopting the
    stored triplet is what makes a progressed replay independent of external progress, but
    it must not make a foreign manifest adoptable: a triplet whose workspace is not this
    transaction's names a launch in someone else's workspace, and no amount of "it was
    already stored" makes that this action's evidence.

    Only the workspace axis, deliberately. The cause vocabulary belongs to the provider
    registry, and the planner already validated it against the registry when this row was
    FIRST planned; re-deriving it here would put a second owner of that token in e_110 and
    would re-do exactly the re-reading this correction exists to remove. An empty triplet is
    a legacy participant and is never foreign.
    """
    if pin is None:
        return False
    if not pin.evidence_workspace_id:
        return False
    return pin.evidence_workspace_id != norm(workspace_id)



__all__ = (
    "participant_authority_matches",
    "participant_with_stored_evidence",
    "stored_evidence_is_foreign",
    "supersede_participant_signature",
)
